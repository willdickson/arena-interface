# Wire Protocol

This page describes the USB-serial protocol spoken by the G6 arena controller
firmware (Teensy 4.1). It was derived from `reference/firmware/` (the source of
truth) and cross-checked against the JavaScript host in `reference/js/`.
The panel firmware in `reference/panel/` is the authority on how pixel bytes
inside a panel block are displayed. Source locations are given as `File:line` where useful.

All multi-byte integers are **little-endian**. "u8/u16/u32/i16" are unsigned /
signed integer widths. Opcodes are written in hex.

## Serial settings

| Setting | Value |
|---|---|
| Transport | USB CDC-ACM (Teensy 4.1 native USB, `Serial`) |
| Baud rate | Ignored by USB CDC. Firmware calls `Serial.begin(115200)`; the host may open at any rate (use 115200 by convention). |
| Data bits / parity / stop | Irrelevant on USB CDC (8N1 by convention). |
| Flow control | None. |
| USB VID/PID | `16C0:0483` (PJRC "Teensyduino Serial", the stock Teensy USB-serial IDs; confirmed with `lsusb` on a real controller). These identify *any* Teensy in serial mode, not specifically this controller. |
| Ethernet | The firmware also has a TCP path (port 62222) with identical framing, but `net.begin()`/`serviceTcp()` are commented out in `main.cpp`. Out of scope for this library. |

Only one process can hold the port at a time.

## Arena variants

One firmware image targets exactly one arena geometry, chosen at build time
(`constants.h:47-59`). The host learns it from `GET_FIRMWARE_VERSION` (rows, cols).

| Build flag | Rows × cols | Panels | Stream frame GS2 / GS16 (bytes) | Default refresh GS16 / GS2 (Hz) |
|---|---|---|---|---|
| `ARENA_HW_2_10` | 2 × 10 | 20 | 1064 / 4064 | 300 / 1000 |
| `ARENA_HW_10_10` | 4 × 10 | 40 | 2124 / 8124 | 400 / 1200 |
| `ARENA_HW_12_18` | 4 × 12 | 48 | 2548 / 9748 | 400 / 1200 |

Stream frame size = 4 + panels × block, with block = 53 B (GS2) or 203 B (GS16).

## Message framing

The link is a raw byte stream; there are no delimiters or checksums at the
USB level. Framing is decided by the **first byte** of each host message
(`SerialManager.cpp:parseIncoming`).

### Host → device

| First byte | Form | Layout |
|---|---|---|
| `0x00`–`0x31` | **Binary command** | `[len, cmd, params…]` — `len` = number of bytes after it (1 + params). Max `len` is `0x31`, i.e. at most 48 parameter bytes. |
| `0x32` | **Stream frame** | `[0x32, len_lo, len_hi, payload…]` — `len` = payload byte count (u16). |
| `0x83` | **Rename (opcode-first)** | `[0x83, idx u16, name_len u8, name…]` — `name_len` 1–63. |
| `0x85` | **Pattern upload (bulk)** | `[0x85, idx u16, size u64, data…]` — 11-byte header then `size` raw bytes. |
| `0xE0` | **Firmware upload (bulk)** | `[0xE0, size u64, data…]` — 9-byte header then `size` raw bytes. |
| anything else | — | The device **discards its whole receive buffer** to resync. No reply. |

Notes:

- The binary length byte doubles as the discriminator, which is why
  commands that may exceed 48 parameter bytes use opcode-first framing.
- Only the low 32 bits of the u64 `size` fields are used (`SerialManager.cpp:105,133`).
- `SET_PATTERN_FILENAME` (0x83) is accepted in **both** binary framing (when it
  fits) and opcode-first framing. The library should always use opcode-first.
- A stream frame whose claimed length exceeds the receive buffer, or a 0x83
  frame with `name_len` 0 or ≥ 64, makes the device silently discard its
  receive buffer. **No reply is sent**; the host sees a timeout.

### Device → host: response frame

Every dispatched command produces exactly one response frame:

```
[len, status, echo_cmd, payload…]
```

- `len` = bytes after it (2 + payload). Response buffer is 200 bytes, so the
  payload is at most 196 bytes and `len` ≤ 198. If a handler's payload would not
  fit, the payload is **silently dropped** and only `[2, status, echo]` is sent
  (`SerialManager.cpp:sendResponse`). No current handler hits this.
- `status` = 0 on success, non-zero on failure (see [Error handling](#error-handling)).
- `echo_cmd` = the request opcode (0x32 for stream frames).
- `payload` = either raw binary fields (GET-style commands) or an **ASCII
  message without a NUL terminator** (`MessageSource.h:sendResponse(const char*)`
  uses `strlen`). An empty message (`""`) produces no payload.

There is no request id. Correlation is by `echo_cmd`, so the host must keep
**one request in flight at a time** (see [Concurrency](#concurrency)).

### Device → host: bulk data

`GET_PATTERN_FILE` (0x84) and `GET_SD_ARCHIVE` (0x8A) send a normal response
frame whose payload is a u64 `size` (status 0), **followed immediately by
`size` raw bytes** with no framing. If the transfer aborts (host stalls > 2 s,
SD read error, 60 s idle) the device just stops sending; there is no trailer
or error frame. The host detects failure by receiving fewer than `size` bytes.

### Device → host: diagnostic text (DEBUG_SERIAL builds only)

Debug builds can interleave text lines on the same pipe. Each line starts with
the sentinel byte **`0xFF`** and ends with `\n` (`constants.h:diag_line_sentinel`,
`main.cpp:SentinelPrint`). `0xFF` can never be a response length byte (max 198),
so the host can demultiplex: a frame that begins with `0xFF` is a diagnostic
line to be read up to `\n`. Diagnostics are off at boot and controlled by
`SET_DIAG_OUTPUT` (0xC3); boot-time banner lines are printed unconditionally
in debug builds. Release builds never emit them. The flag bit
`GET_FIRMWARE_VERSION.flags bit1` tells the host whether it is a debug build.

## Device state

The controller is always in one of these states (`CommandProcessor.h:ArenaState`;
reported as `GET_HEALTH.state`):

| Value | State | Entered by |
|---|---|---|
| 0 | `ALL_OFF` | boot, 0x00, 0x30, trial duration expiry, end of error glyph, bad stream size |
| 1 | `ALL_ON` | 0xFF |
| 2 | `STREAMING_FRAME` | 0x32 |
| 3 | `OPEN_LOOP` (mode 2) | 0x08 mode 2 |
| 4 | `SHOW_FRAME` (mode 3) | 0x08 mode 3, 0x03, 0x70 |
| 5 | `CLOSED_LOOP` (mode 4) | 0x08 mode 4 |
| 6 | `PSRAM_PLAY` | 0x3A, 0x3B |
| 7 | `ERROR_DISPLAY` | many errors (see below); auto-returns to `ALL_OFF` after 750 ms |

Entering `ALL_OFF` sends a dark frame three times so the panels actually blank
(panels hold their last frame otherwise).

Several commands are refused unless the state is `ALL_OFF` (status 10,
`CE_DISPLAY_ACTIVE`), and several are refused while an SD transfer
(0x84 / 0x85 / 0x8A) is running (status 1, "SD transfer in progress").

## Commands

Request bytes are shown for binary framing (`[len, cmd, …]`). "Reply" lists the
success payload; on failure the payload is an ASCII message unless noted.

### Display control

| Op | Name | Request | Reply payload | Notes |
|---|---|---|---|---|
| 00 | ALL_OFF | `01 00` | "All-Off Received" | Always accepted. Clears per-trial duty. |
| FF | ALL_ON | `01 FF` | "All-On Received" | Refused during SD transfer. GS16, all pixels max. |
| 30 | STOP_DISPLAY | `01 30` | "Display has been stopped" | Same effect as ALL_OFF. |
| 08 | TRIAL_PARAMS | `0C 08 mode pat:u16 rate:i16 init:u16 gain:i16 dur:u16` (+ `duty:u8` → `0D`) | empty | Start mode 2/3/4 with 1-based SD pattern. `rate` Hz (mode 2; negative = reverse). `init` 0-based start frame (taken modulo frame count). `gain` = 10× fps/V (mode 4). `dur` in 10 ms ticks, 0 = run until stopped. `duty` 0 = pattern's stored duty, 1–255 = override for this trial. Errors: < 11 params, mode ∉ {2,3,4}, or pattern load failure → **error glyph** + status 1. Refused during SD transfer. |
| 03 | SET_PATTERN_ID | `03 03 id:u16` | `id:u16` (echo) | Open pattern in SHOW_FRAME at frame 0, no auto-advance, stored duty. Load failure → error glyph + status 1. |
| 70 | SET_FRAME_POSITION | `03 70 index:u16` | empty | Show 0-based frame of the open pattern; switches state to SHOW_FRAME. Errors (no pattern open, index ≥ frame count, SD read failure) → **error glyph** + status 1. |
| 72 | GET_FRAME_POSITION | `01 72` | `index:u16, frame_count:u16` | 0/0 when no pattern is open. |
| 32 | STREAM_FRAME | `32 len:u16 payload` | empty (echo 0x32) | Payload = 4-byte prefix (`'F','R', frame_index:u16`) + one block per panel, row-major. Size must be exactly the GS2 or GS16 stream size for this arena; otherwise device goes to ALL_OFF and replies status 1 "Bad stream-frame size". Enters STREAMING_FRAME. |
| 3A | DISPLAY_PSRAM_INDEX | `03 3A index:u16` | empty | Panels show their locally stored PSRAM frame `index`. Too short → error glyph. |
| 3B | PSRAM_PLAY | `07 3B start:u16 count:u16 fps:u16` | empty | Auto-advance PSRAM frames `[start, start+count)` at `fps`. `count` 0 is treated as 1. |
| 16 | SET_REFRESH_RATE | `03 16 hz:u16` | empty | Panel re-transmit rate. `hz` 0 is accepted but ignored. Once set, it overrides the per-grayscale defaults until reboot. |
| 17 | GET_REFRESH_RATE | `01 17` | `hz:u16` | |
| 1B | SET_PANEL_DISPLAY_MODE | `02 1B mode` | `mode:u8` (echo) | 0 oneshot, 1 persist (default), 2 triggered, 3 gated. Sticky; applied to the current frame immediately. |
| 1C | GET_PANEL_DISPLAY_MODE | `01 1C` | `mode:u8` | |
| 33 | GET_FRAMES_SENT | `01 33` | `count:u32` | Frames pushed to panels since boot or reset. |
| 34 | RESET_FRAMES_SENT | `01 34` | empty | |
| C5 | SET_SPI_CLOCK | `03 C5 mhz:u16` | `applied_mhz:u16` | Firmware clamps to 1–30 MHz and echoes the applied value. |
| C6 | GET_SPI_CLOCK | `01 C6` | `mhz:u16` | |

### SD card / pattern library

Patterns are `/patterns/*.pat`, sorted alphabetically, addressed by a
**1-based index** (max 256 listed). Index 0 means the staging file
`/patterns/pattern.temp` where noted. Renames and deletes **re-sort / compact the
index space**.

| Op | Name | Request | Reply payload | Notes |
|---|---|---|---|---|
| 80 | GET_FILE_COUNT | `01 80` | `count:u16` | |
| 82 | GET_PATTERN_FILENAME | `03 82 idx:u16` | `name_len:u8, name` | Status 1 "Index out of range". |
| 88 | GET_PATTERN_INFO | `03 88 idx:u16` | `frame_count:u16, gs:u8, rows:u8, cols:u8, arena_id:u8, observer_id:u8, file_size:u32, duty_cycle:u8` (12 B) | `gs` 1 = GS2, 2 = GS16. `duty_cycle` = frame 0 / panel 0. Allowed while displaying. Failure status = CE code. |
| 83 | SET_PATTERN_FILENAME | `83 idx:u16 name_len:u8 name` | `new_idx:u16` | Rename; idx 0 promotes `pattern.temp`. `name_len` 1–63. Refused during SD transfer. Failure status = CE code. |
| 84 | GET_PATTERN_FILE | `03 84 idx:u16` | `size:u64`, then `size` raw bytes | Requires ALL_OFF. See [bulk data](#device-host-bulk-data). |
| 85 | SET_PATTERN_FILE | `85 idx:u16 size:u64 data` | empty, sent **after** all data is received | idx 0 writes `pattern.temp`, idx ≥ 1 overwrites that pattern. Requires ALL_OFF. `size` 0 → immediate status 1. On any other rejection the device still **reads and discards all `size` bytes** before replying. Idle timeout 30 s. |
| 86 | DELETE_PATTERN_FILE | `03 86 idx:u16` | empty | idx 0 deletes `pattern.temp`. Failure status = CE code. |
| 8F | PURGE_MEMORY | `01 8F` | empty | **Formats the whole SD card** (patterns, panel firmware, manifests). Requires ALL_OFF and no transfer. Slow (scales with card size). |
| 8A | GET_SD_ARCHIVE | `01 8A` | `size:u64`, then a ZIP (store mode) | Requires ALL_OFF. Contains `MANIFEST.bin`, `MANIFEST.txt` and `patterns/*.pat`. |
| CD | GET_SD_INFO | `01 CD` | 30 B: `ver, flags, card_type, fat_type, sectors:u32, bytes_per_cluster:u32, cid[16], sd_status_maint, sd_diag` | **Status 1 still carries the payload** (no card mounted). |
| CE | SET_SD_DIAG | `02 CE flags` | `flags:u8` | Bench switches: bit0 legacy seek, bit1 no same-index skip. Bits 2–7 must be 0. |

### Analog / digital I/O

| Op | Name | Request | Reply payload | Notes |
|---|---|---|---|---|
| A0 | SET_AO_VOLTAGE | `03 A0 mv:u16` | `mv:u16` | 0–5000 mV on the analog-out BNC. Stops LUT playback. Refused in AO mode 1. |
| A1 | GET_AO_VOLTAGE | `01 A1` | `mv:u16` | **Read back from the DAC** (12-bit quantized), not the commanded value. |
| A2 | SET_AO_LUT | `len A2 mode step_hz:u16 count:u16 mv:u16×count` | `count:u16` | mode 0 = frame-locked, 1 = time-based at `step_hz` (≤ 1000). Each mv 0–5000. **`count` ≤ 21** (binary framing limit, see [Quirks](#quirks)). Refused in AO mode 1. |
| A3 | SET_AO_MODE | `02 A3 mode` | empty | 0 programmable, 1 frame_number (DAC tracks frame index, 0–5 V across the pattern). |
| A4 | GET_ANALOG_IN | `01 A4` | `ain1_mv:i16, ain2_mv:i16` | ±10 V inputs, 10-bit ADC, uncalibrated. |
| AA | SET_DIGITAL_OUT | `03 AA channel state` | empty | channel 1/2; state 0/non-zero. A port in role `off` is auto-promoted to `out_programmable`; a port in `in_trigger` or `out_debug_framescan` is refused (status 1). |
| AB | GET_DIGITAL_OUT | `01 AB` | `level1:u8, level2:u8` | Live pin reads. |
| AC | SET_DIO_ROLE | `03 AC port role` | empty | port 1/2; role 0 off, 1 in_trigger, 2 out_programmable, 3 out_debug_framescan. Boot defaults: port 1 = out_programmable (LOW), port 2 = in_trigger. |
| AD | GET_DIO_ROLE | `01 AD` | `role1, level1, role2, level2` | |

### Identity, health and diagnostics

| Op | Name | Request | Reply payload | Notes |
|---|---|---|---|---|
| C2 | GET_CONTROLLER_INFO | `01 C2` | `version:u8, capability:u8, mac[6]` | `version` = 1. This firmware reports capability `0xA3`. See [capability bits](#capability-and-feature-flags). |
| CB | GET_FIRMWARE_VERSION | `01 CB` | 46 B: `ver, rows, cols, flags, sha[8], date[10], branch[24]` | ASCII fields right-padded with spaces. Gated by capability bit 7. |
| CA | GET_HEALTH | `01 CA` | 114 B (`ver` = 5) | Cumulative counters, previous-boot breadcrumb, watchdog state. Layout in `CommandProcessor.cpp:1078-1128`; offsets never move between versions. Gated by capability bit 7. |
| CC | GET_CRASHREPORT | `01 CC` | 128 B raw | PJRC `arm_fault_info_struct` (44 B) + PJRC breadcrumbs. Not cleared by reading. Gated by firmware flags bit 3. |
| A8 | SET_TELEMETRY | `02 A8 flags` or `04 A8 flags rate:u16` | empty | bit0 record events (on at boot); bit7 synthetic producer at `rate`/s; bit4 = watchdog bits present, then bit6 watchdog off, bit5 starve watchdog (**resets the controller after 2 s**). Gated by firmware flags bit 2. |
| A9 | GET_TELEMETRY_BLOCK | `08 A9 ack_seq:u32 max_bytes:u16 flags` | 18-byte header + whole records | Frees records with `seq ≤ ack_seq` (0xFFFFFFFF = no ack), then returns up to min(max_bytes, 178) bytes of records **without** freeing them. Record format below. |
| C1 | GET_ETHERNET_IP_ADDRESS | `01 C1` | ASCII dotted quad | Ethernet is not started in this build, so the value is not meaningful. |
| C3 | SET_DIAG_OUTPUT | `02 C3 on` (or `01 C3` = on) | "diag on" / "diag off" | Only has an effect on DEBUG_SERIAL builds. |
| C4 | GET_DIAG_OUTPUT | `01 C4` | `on:u8` | |
| 01 | SYSTEM_RESET | `01 01` | "rebooting" | Replies, then resets ~10 ms later. USB drops and re-enumerates. |

### Panel firmware (in-system programming)

| Op | Name | Request | Reply payload | Notes |
|---|---|---|---|---|
| E0 | SET_FIRMWARE_FILE | `E0 size:u64 data` | `crc32:u32` (CRC-32/ISO-HDLC of stored bytes) | Writes `/firmware/panel.bin`. Requires ALL_OFF and no transfer. Blocking; idle timeout 30 s. |
| E3 | GET_FIRMWARE_INFO | `01 E3` | 32 B footer: `magic[8], version[16], crc32:u32, size:u32` | Status 1 if no image. |
| C8 | G6_PROGRAM_PANEL | `02 C8 panel_number` | ASCII result message | 1-based panel number. Requires ALL_OFF. Blocks for several seconds. |
| C9 | G6_VERIFY_PANEL | `02 C9 panel_number` | ASCII result message | Status 0 = CRC match. Requires ALL_OFF. |

### Reserved / rejected opcodes

`06` SWITCH_GRAYSCALE replies status 1 with an explanation (dropped for G6).
`C0`, `C7`, `71`, `73` are defined in `commands.h` but not implemented. They,
and **every other unknown opcode**, are handled as an unknown command:
**the arena shows a "CE 01" error glyph** for 750 ms (which stops any running
display) and the reply is status 1 "Unknown command". Hosts must therefore
**not probe** for optional commands; they must gate on the capability/feature
flags below.

## Capability and feature flags

`GET_CONTROLLER_INFO.capability` (`constants.h:187`):

| Bit | Name | Meaning |
|---|---|---|
| 0 | g6_mode | Always 1 on G6 |
| 1 | v2_local_storage | |
| 2 | mode_1_tsi | |
| 3 | v3_triggered | |
| 4 | v3_gated | |
| 5 | io_ext | 0xA3, 0xA4, 0xAC, 0xAD present |
| 6 | ai_cal | (claimed by a different firmware branch; not set here) |
| 7 | health | 0xCA and 0xCB present |

`GET_FIRMWARE_VERSION.flags` (`Version.h`):

| Bit | Meaning | Gates |
|---|---|---|
| 0 | Dirty working tree at build | — |
| 1 | DEBUG_SERIAL build (diagnostic text may appear) | — |
| 2 | Telemetry ring present | 0xA8, 0xA9 |
| 3 | Crash report + health v2 present | 0xCC |
| 4 | Free-running refresh timer | — |
| 5 | SD fast path | 0xCD |
| 6 | SD diagnostics | 0xCE |

## Pattern file format (.pat)

Patterns on the SD card (and uploaded with 0x85) use the G6 v2 format. The
layout below is what the firmware validates (`SdManager.cpp:validateHeader`,
`readFrame`) and what `reference/js/pat-encoder.js` / `pat-parser.js` write and
read. The firmware passes panel blocks to the panels unchanged, apart from the
display-mode bits and an optional per-trial duty override.

### Header (18 bytes)

| Offset | Field |
|---|---|
| 0–3 | Magic `"G6PT"` |
| 4 | bits 7–4: format version (**must be 2**); bits 3–0: arena_id bits 5–2 |
| 5 | bits 7–6: arena_id bits 1–0; bits 5–0: observer_id |
| 6–7 | `frame_count:u16` (must be > 0) |
| 8 | panel rows |
| 9 | panel cols |
| 10 | `gs`: 1 = GS2 (on/off), 2 = GS16 (16 levels) |
| 11–16 | panel mask, 48 bits, bit *i* = panel *i* (byte 11 bit 0 = panel 0) |
| 17 | CRC-8/AUTOSAR over bytes 0–16 (poly 0x2F, init 0xFF, xorout 0xFF, not reflected; check "123456789" → 0xDF) |

The firmware rejects a file unless rows × cols equals this build's arena
(`CE_ARENA_MISMATCH`), the mask has at most rows × cols bits set, and the
magic, version, CRC-8, frame count and `gs` are valid (`CE_HEADER_CRC`).
arena_id and observer_id are host metadata only. The JS parser also reads an
older 17-byte v1 header; the firmware does not accept v1.

### Frames

`frame_count` frames follow the header back to back. Each frame is:

| Part | Size | Content |
|---|---|---|
| Prefix | 4 | `'F','R', frame_index:u16` |
| Panel blocks | panels × 53 (GS2) or × 203 (GS16) | One block per panel, row-major, panel row 0 = **bottom** of the arena |
| Trailer | 2 | CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no xorout; check → 0x29B1) over prefix + blocks, little-endian |

The firmware checks the `"FR"` magic and the CRC-16 of every frame it
displays (`CE_FRAME_CRC`). It does not check `frame_index`.

A **stream frame** payload (0x32) is exactly one frame without its trailer.

### Panel block

| Offset | GS2 (53 B) | GS16 (203 B) |
|---|---|---|
| 0 | header: bits 6–0 = protocol version 1, bit 7 = even parity | same |
| 1 | display opcode `0x10` (written as oneshot) | `0x30` |
| 2… | 50 bytes, 1 bit/pixel, MSB first | 200 bytes, 4 bits/pixel, even pixel in high nibble |
| last | `duty_cycle` (brightness 0–255; 0 = off) | same |

- **Parity:** bit 7 of byte 0 is chosen so the number of 1 bits in
  {byte 0 bits 6–0, all following bytes} is even (`G6PanelProtocol.h:stamp_header_parity`).
- **Opcode:** the low 2 bits (display mode) are rewritten by the firmware at
  display time from SET_PANEL_DISPLAY_MODE, so the file value does not matter.
- **Duty:** the JS encoder writes 128 by default. A trial's `duty` parameter
  (0x08) overrides it at display time.
- **Pixel order:** row-major, **starting at the top row of the panel**:
  `pixel = row_from_top × 20 + col`, col 0 = left. This is what the panel
  firmware does (`reference/panel/message.cpp:to_pattern_gray_2/16` unpacks
  pixel *n* into matrix row *n* / 20; `isp_logic.h:74` states "Row 0 = top"
  for the matrix, and its built-in ERR and smiley glyphs read upright with
  that convention). The `.pat` encoder in `pat-encoder.js` follows it too.
  **Confirmed on hardware:** the controller's error glyph, which is packed
  bottom row first, shows on the panels upside down with the digits above "CE".
  Across the arena, panel blocks are in row-major order starting from the
  **bottom** row of panels, columns left to right, so block *k* is panel
  number *k* + 1 (as used by PROGRAM_PANEL / VERIFY_PANEL). A host image with
  row 0 at the bottom must therefore flip rows *within* each panel when
  packing. **Confirmed on hardware** (2×10 build, panels 4 and 5 fitted,
  2026-11-04): the image's bottom panel row, column 3 lit panel 4; the top
  panel row lit nothing; the top-left 6×6 pixels of each panel lit the
  top-left corner of panels 4 and 5; panel 4 is left of panel 5.

## Telemetry records

`GET_TELEMETRY_BLOCK` reply header (18 B): `t_now_us:u32, first_seq:u32,
n_records:u16, dropped:u32, more:u8, flags:u8, boot_count:u16`. Header flags:
bit0 events enabled, bit1 ring survived a reboot, bit2 ring disabled (heap
collision), bit3 synthetic producer on.

Records follow back to back (`Telemetry.h`):

| Offset | Field |
|---|---|
| 0 | `len:u8` (whole record) |
| 1 | `type:u8` — 1 CMD, 2 FRAME, 3 STATE (0 PAD is never returned) |
| 2 | `seq:u32` |
| 6 | `t_us:u32` (raw `micros()`; wraps every ~71.6 min) |
| 10 | type-specific payload |

- **CMD** (13–21 B): `cmd:u8, status:u8, plen:u8, request_bytes[plen ≤ 8]`.
- **FRAME** (26 B): `idx:u16, pattern:u16, sd_load_us:u32, spi_us:u16,
  req_age_us:u32, superseded:u8, flags:u8`.
- **STATE** (14 B): `kind:u8, code:u8, arg:u16`; kinds 1–14 as listed in
  `Telemetry.h:118-145`.

The intended drain loop: send `ack_seq = 0xFFFFFFFF` first, then
`ack_seq = first_seq + n_records - 1` of the last block received, repeating
while `more` is 1. A lost reply is recovered by resending the same ack.

## Asynchronous messages

**None.** The firmware never sends unsolicited frames. Every frame is a reply
to a command, except:

- raw bulk bytes after a 0x84 / 0x8A header (solicited), and
- `0xFF`-prefixed diagnostic text lines on DEBUG_SERIAL builds.

Anything "streamed" from the device (frame position, analog inputs, health,
telemetry events) must be **polled** by the host. The telemetry ring is the
designed mechanism for a complete event log (commands, frame changes, state
changes) with no gaps.

## Error handling

### Status codes

| Status | Name | Typical source |
|---|---|---|
| 0 | OK | |
| 1 | generic error / `CE_UNKNOWN_CMD` | Most validation failures (message in payload); unknown opcode |
| 2 | `CE_BAD_PAYLOAD_LEN` | (used as glyph code; replies use 1) |
| 3 | `CE_BAD_PARAM` | Index out of range (SD manager) |
| 4 | `CE_SD_NOT_PRESENT` | No SD card |
| 5 | `CE_SD_FILE_ERROR` | File missing / unreadable / rename or delete failed |
| 6 | `CE_HEADER_CRC` | Bad pattern header (magic, version, CRC-8) |
| 7 | `CE_FRAME_CRC` | Bad frame CRC-16 |
| 8 | `CE_ARENA_MISMATCH` | Pattern geometry ≠ this arena |
| 9 | `CE_MANIFEST_WRITE_ERROR` | Manifest update failed |
| 10 | `CE_DISPLAY_ACTIVE` | Command needs ALL_OFF |
| 11 | `CE_SD_FORMAT_ERROR` | Purge (format) failed |

Codes 2–11 appear as reply statuses only from the SD handlers (0x83, 0x86,
0x88, 0x8F) and from `CE_DISPLAY_ACTIVE` guards. Display-start failures
(0x08, 0x03, 0x70) always reply status 1, but show the precise CE code as a
glyph on the arena. The ASCII payload is the only way to tell status-1
errors apart.

### Error glyph side effect

These failures put the arena into `ERROR_DISPLAY` ("CE NN" on every panel)
for 750 ms, then `ALL_OFF`, **stopping whatever was being displayed**:
unknown opcodes; TRIAL_PARAMS too short / bad mode / load failure;
SET_PATTERN_ID load failure; SET_FRAME_POSITION with no pattern, index out of
range, or read failure; DISPLAY_PSRAM_INDEX / PSRAM_PLAY too short. The
library should validate arguments locally so that these are not triggered by
host bugs.

### Malformed input

| Input | Device behaviour |
|---|---|
| Unknown first byte | Receive buffer discarded, no reply → host timeout |
| Stream frame too large for buffer | Receive buffer discarded, no reply |
| Stream frame of wrong (but bufferable) size | ALL_OFF, status 1 |
| 0x83 with `name_len` 0 or ≥ 64 (opcode-first) | Receive buffer discarded, no reply |
| Binary command with wrong parameter count | Status 1 with "Expected […]" message (per handler) |

### Recovery

After a timeout, stale bytes may still arrive. The host should discard its
receive buffer (the JS host does this) before sending the next request.

## Timing

| Item | Value | Source |
|---|---|---|
| Normal command reply | well under 1 ms of device time; JS uses a 500 ms timeout | `arena-link.js:70` |
| PURGE_MEMORY (0x8F) | tens of seconds to minutes (SD format); JS uses 120 s | `arena-wire-g6.js:583` |
| G6_PROGRAM_PANEL / VERIFY (0xC8/0xC9) | several seconds | `CommandProcessor.cpp:2729` |
| GET_SD_ARCHIVE (0x8A) header | proportional to file count (stat of up to 258 files) | |
| Bulk download (0x84/0x8A) | device aborts after 2 s with no host read progress, or 60 s idle | `SerialManager.cpp:sendRaw` |
| Bulk upload (0x85) | 30 s idle → abort and drain (≤ 5 s more), then status 1 | `CommandProcessor.h:259` |
| Rejected bulk upload | device drains: 5 s idle / 15 s absolute | `CommandProcessor.cpp:drainBulkData` |
| Firmware upload (0xE0) | 30 s idle | |
| SYSTEM_RESET | reply then reset after ~10 ms; USB re-enumerates (~1–3 s) | |
| Boot | panels blanked at boot and again 3.5 s after reset | `constants.h:panel_late_boot_blank_ms` |
| Error glyph hold | 750 ms, then ALL_OFF | `constants.h:error_display_hold_ms` |
| Trial duration tick | 10 ms | `constants.h:duration_tick_ms` |
| Mode 4 AIN sampling | 500 Hz | |
| Hardware watchdog | 2 s (30 s during long ops) — a wedged controller resets itself | `Health.h` |

### Refresh rate and measured performance

The **refresh rate** is how often the controller re-sends the current frame
to all panels over SPI (a timer in the controller firmware). A new frame
reaches the panels at the next refresh tick. Defaults (`constants.h`): 2x10
builds 300 Hz for GS16 content and 1000 Hz for GS2; other builds 400 / 1200 Hz.
The controller switches between the GS2 and GS16 defaults automatically,
until the host sets a rate with SET_REFRESH_RATE: that rate then applies to
all content until the controller reboots. Separately, each panel scans its
LEDs at a fixed 1 kHz (`reference/panel/display.cpp`), which is not
adjustable from the host; frame rates above 1 kHz bring no visible benefit.

Measured on a 2x10 controller (firmware `12bba7e9`, debug build, 25 MHz SPI,
Linux host in a VM, 2026-11-05). No panel errors were seen at any rate.

| Refresh (set) | GS16 transfers achieved | GS2 transfers achieved |
|---|---|---|
| 300-1250 Hz | as set | as set |
| 1500 Hz | 1282/s (SPI saturated) | as set |
| up to 3000 Hz | — | as set |

Host-paced frames, sent as fast as possible ("shown" = distinct frames that
reached the panels; for streamed frames, at most min(sends, transfers)):

| Method | Refresh | Commands/s | Frames shown/s |
|---|---|---|---|
| SET_FRAME_POSITION (SD, GS16) | 300 Hz | 571 | 298 |
| | 500 Hz | 478 | 473 |
| | 1000 Hz | 353 | 352 |
| STREAM_FRAME GS16 (4064 B) | 300 Hz | 1107 | ≤ 300 |
| | 1000 Hz | 951 | ≤ 951 |
| | 1250 Hz | 415 | ≤ 415 |
| STREAM_FRAME GS2 (1064 B) | 1000 Hz | 2340 | ≤ 1000 |

Each refresh transfer blocks the controller's main loop (about 0.7 ms for a
2x10 GS16 frame), so higher refresh rates leave less time for commands; at
1250 Hz and above, GS16 transfers saturate SPI and command handling drops
sharply. A streamed frame takes effect when it arrives, so it is displayed
if the host sends frames no faster than the refresh rate (each frame then
stays in place for at least one refresh period). An SD frame
(SET_FRAME_POSITION) takes effect only after the controller has read it from
the card, which usually takes about 1.7 ms but occasionally stalls; at fps
close to the refresh rate a delayed frame can then be replaced before it is
shown (seen once: 1 of 99 frames at 200 fps with a 300 Hz refresh). For SD
playback that must show every frame, keep fps well below the refresh rate or
raise the refresh rate, and check with `play_frames(..., verify=True)`.

### Concurrency

- The device parses one command at a time and has **one response buffer**. A
  response that the host has not drained yet can be **overwritten** by the next
  command's response. The host must wait for each reply before sending the next
  command (single flight).
- During a bulk transfer the device does not process other commands on the
  serial link. The host must not send anything until the transfer completes.

## Quirks

1. **Binary command length limit.** Length byte must be ≤ `0x31`, so a binary
   command carries at most 48 parameter bytes. SET_AO_LUT therefore accepts at
   most 21 entries per command, although the firmware buffer holds 4096
   (`CommandProcessor.cpp:938` checks only `needed ≤ 255`).
2. **Display keeps running after the host disconnects.** USB CDC disconnect
   does not change the device state. A "safe" close must send ALL_OFF (see
   [Host-side decisions](#host-side-decisions)).
3. **GET_SD_INFO** returns its full payload with status 1 when no card is
   mounted.
4. **SET_REFRESH_RATE is sticky** for the rest of the boot: after one explicit
   rate, the per-grayscale defaults are no longer applied.
5. **Index shifting.** DELETE and RENAME re-sort the 1-based pattern indices;
   cached index → name maps must be refreshed afterwards.
6. **Patterns must match the arena.** A pattern whose rows × cols differ from
   the firmware build is rejected (`CE_ARENA_MISMATCH`).
7. **Status 1 is overloaded.** It means both "generic error" and
   `CE_UNKNOWN_CMD`; only the ASCII message distinguishes them.
8. Bulk download failures are silent: the only signal is a short byte count.

## Discrepancies

Firmware and JS disagree in the places below. The library follows the firmware.

| Topic | Firmware | JS | Decision |
|---|---|---|---|
| Commands 0xD0/0xD1 (panel inventory), 0xB0/0xB1 (I2C bridge), 0xA5–0xA7 (analog cal), feature bitmap after MAC in 0xC2 | Not present | Encoded/decoded | Omit (they come from a newer firmware). Sending them to this firmware would trigger a CE 01 glyph. |
| SET_AO_LUT (0xA2), SET_PATTERN_ID (0x03), PSRAM (0x3A/0x3B) | Implemented | Not encoded | Implement. |
| GET_AO_VOLTAGE meaning | DAC hardware readback (quantized) | "last commanded level" | Document as readback. |
| GET_ANALOG_IN length | 4 bytes | Optional 5th flags byte | Decode 4 bytes; tolerate a longer payload. |
| GET_PATTERN_INFO byte 11 | `duty_cycle` | `stretch` | Name it `duty_cycle`. |
| GET_HEALTH size/version | ver 5, 114 bytes (code); header comment still says ver 1 / 66 B | Decodes 55 B base + tails up to v5 | Decode by length like the JS, documenting v5. |
| GET_CRASHREPORT gate | flags bit 3 | One comment says bit 2, another bit 3 | Bit 3. |
| SET_SD_DIAG gate | flags bit 6 | Comment says bit 5 | Bit 6. |
| GET_TELEMETRY_BLOCK max_bytes | Capped at 178 | Default 180 | Default 178. |
| SET_SPI_CLOCK range | Clamps to 1–30, echoes applied | Throws outside 1–30 | Validate 1–30 locally (clearer errors); return the applied value. |
| Comments on 0xC1/0xC2 encoders | — | Say `01 C0` / `01 C1` | JS comment typos; opcodes are 0xC1 / 0xC2. |
| Stream frame sizes | Depend on build (20/40/48 panels) | Default 20 panels | Derive from GET_FIRMWARE_VERSION rows × cols. |
| Pixel row order inside a panel block | Controller passes blocks through. **Panel firmware** (`reference/panel/`): first 20 pixels = **top** row. But the controller's own error glyph (`ErrorGlyph.cpp:72-82`) packs the **bottom** row first, citing the g6_04 spec ("origin bottom-left, pixel_num = row_from_bottom × 20 + col"). | `pat-encoder.js`/`pat-parser.js`: top row first. `g6-encoding.js` ("matches MATLAB `g6_encode_panel.m`"): bottom row first. | **Top row first**, as the panel firmware does (matches `pat-encoder.js`). Confirmed on hardware: the controller's "CE NN" glyph appears vertically flipped (upside-down digits above "CE"). This is a controller-firmware bug in `ErrorGlyph.cpp`, and the g6_04 spec text and `g6-encoding.js` are out of step with the panel firmware. |
| Partial arenas (panel mask with gaps) | Mask popcount checked only | Encoder assumes full grid | Full grid only for now. |

## Host-side decisions

Behaviour the library adds on top of the wire protocol, as agreed in review.

- **Close sends ALL_OFF by default.** Closing the connection (explicitly or by
  leaving a `with` block, including on an exception) sends ALL_OFF before
  releasing the port, so the arena goes dark. This can be turned off per
  connection with `Device.open(..., all_off_on_close=False)` (or the
  `all_off_on_close` attribute), for example to leave a stimulus running after a script exits.
  The ALL_OFF is best-effort: if the device has already disappeared, close
  still releases the port and does not raise.
- **Patterns are built from pixel arrays.** The library builds and parses
  `.pat` files and stream-frame payloads from whole-arena pixel arrays
  (GS2: 0/1, GS16: 0–15), handling panel splitting, row order within panels,
  packing, parity and CRCs. numpy is a required dependency (decided
  2026-11-06; it was an optional extra before): images may be numpy arrays
  or nested lists, and frames are returned as uint8 numpy arrays.
- **Pixel array orientation is selectable, top-origin by default.** Every
  function that takes or returns a pixel array has an `origin` argument named
  after matplotlib's `imshow`: `"upper"` (default) means row 0 is the top of
  the arena; `"lower"` means row 0 is the bottom (the `pat-encoder.js` /
  MATLAB / G4 convention). Columns run left to right in both. The library
  converts to the on-wire order (top row first within each panel) internally.
- **Auto-detect is optional, and probes by default.** VID/PID `16C0:0483`
  matches any Teensy running USB serial, so:
    - An explicit port skips detection entirely; no other port is touched.
    - `find_ports()` lists VID/PID matches and sends nothing.
    - With no port given, a single match is opened directly. With several
      matches, each is confirmed with `GET_CONTROLLER_INFO` (`01 C2`); this
      probing is on by default and can be turned off with `probe=False`.
      If the choice is still ambiguous, an error lists the candidates and
      asks for an explicit port.
- **Unsupported commands are refused locally.** On connect the library reads
  `GET_CONTROLLER_INFO` (0xC2) and, when capability bit 7 is set,
  `GET_FIRMWARE_VERSION` (0xCB). A method whose command is gated by a
  capability bit or firmware flag that the device does not advertise (see
  [Capability and feature flags](#capability-and-feature-flags)) raises
  `UnsupportedCommand` without sending anything, so it can never trigger the
  CE 01 glyph. Ungated commands are always sent. Opcodes this firmware does
  not implement at all (the reserved ones and the newer-firmware commands
  listed under [Discrepancies](#discrepancies)) are not exposed.
- **Diagnostic and bench commands live in a `diagnostics` namespace.** All
  firmware commands are supported, but the ones meant for bench work and
  post-mortems are grouped under `device.diagnostics` so the main API stays
  focused on running experiments: GET_HEALTH (0xCA), GET_CRASHREPORT (0xCC),
  GET_SD_INFO / SET_SD_DIAG (0xCD / 0xCE), GET/SET_DIAG_OUTPUT (0xC3 / 0xC4)
  and GET/SET_SPI_CLOCK (0xC5 / 0xC6). The telemetry ring (0xA8 / 0xA9) has
  its own `device.telemetry` group (changed in M6: frame and state events are
  useful for ordinary experiment logs, not just debugging). The telemetry
  **watchdog bits**
  (SET_TELEMETRY bits 4–6: disable or starve the watchdog, which resets the
  controller) are **not exposed**; the library always sends them as 0. Panel
  firmware update (0xE0, 0xE3, 0xC8, 0xC9) is maintenance rather than bench
  debugging and gets its own group, `device.panel_firmware`.

- **Arenas are described by geometry, with built-in presets.** An `Arena`
  has a full panel grid (rows × cols), the installed columns, and optionally a
  name and an arena id (written to the `.pat` header `arena_id` field; the
  controller ignores it). The eight G6 arenas from the JS registry are built in
  as presets: 1 `G6_2x10`, 2 `G6_2x8of10`, 3 `G6_3x12of18`, 4 `G6_3x16_full`,
  5 `G6_3x10`, 6 `G6_4x10`, 7 `G6_1x12of18`, 8 `G6_3x10of14`. Other arenas
  are described directly (`Arena(rows, cols, installed_columns=..., arena_id=...)`).
  Loading the maDisplayTools YAML arena registry was planned as an option but
  dropped (2026-11-09); new arenas are added to the built-in list instead.
- **SD patterns can be chosen by file name.** `start_trial`, `show_pattern`
  and the playback methods take `pattern`: a 1-based id or a file name
  (decided 2026-11-09). Names match case-insensitively with `.pat` optional,
  as FAT file names do; an unknown name raises `KeyError`. The firmware only
  loads by id, so a name is resolved by listing the card
  (GET_PATTERN_FILENAME per pattern) on every call, with no cache. Ids shift
  when files are added, renamed or deleted, so names are the stable choice.
- **Patterns are full-grid.** The panel mask is always "all panels"; columns
  that are not installed are left blank. This matches the JS tools
  (`pattern-set.js`: the controller cannot play partial-arena patterns).
- **Rig geometry is mapped onto the firmware build.** The rig (physical arena)
  may be smaller than the grid the controller firmware is built for (e.g. a
  3×10 rig on 4×10 firmware; panels that are not present are ignored). Stream
  frames and patterns encoded for upload use the firmware's grid, following
  `studio-stream-geometry.js`: grid column *c* → firmware column *c* (installed
  columns must fit); rig rows are repeated up the frame to fill the firmware's
  rows. Firmware that does not report its geometry (no capability bit 7) can
  only stream 20-panel arenas unless the user states the geometry. **Row
  repetition vs. blank padding is not yet verified on hardware.**
- **Streaming means polling, exposed as blocking iterators.** The firmware
  sends nothing unsolicited, so:
    - `device.telemetry.events()` drains the telemetry ring continuously and
      yields frame, state and command events. A record is acknowledged only
      after it has been yielded (by the next poll), and the library remembers
      the last sequence number, so stopping and restarting the loop neither
      loses nor repeats events. Sequence gaps (ring overflow) are logged and
      counted.
    - `device.sample(read, rate_hz=...)` calls any read method at a fixed
      rate (drift-free schedule; late samples are skipped and counted).
    - Timestamps are **wall-clock** (`time.time()`). Telemetry times are
      derived from the controller's microsecond clock relative to the
      `t_now_us` in each reply, so the 71.6-minute wrap needs no unwrapping.
      Records written before the controller's last reset (the ring survives
      resets) have no host time. The raw controller time is always kept.
    - Host-paced frame playback (M6b, scope agreed 2026-11-05): both
      pre-computed sequences and live (closed-loop) frame choice; both SD
      frame positions (SET_FRAME_POSITION) and streamed images (STREAM_FRAME);
      late frames are either skipped (keep the schedule) or all sent, chosen
      by the caller; feedback on missed timing is always available; blocking
      and background (thread) playback. No FicTrac bridge for now.
      Implemented as `dev.play_frames()` / `dev.play_images()` (blocking),
      `dev.start_frames()` / `dev.start_images()` (background, returning a
      `Playback`), and `dev.pace()` (live), with `late="skip"|"send"`, an
      optional `refresh_rate=`, a `PlaybackReport` and `verify=True` for SD
      frames; configuration problems raise a `PerformanceWarning`. Measured
      on a 2x10 controller: about 575 frame positions/s (SD-limited) and
      1000-2600 streamed frames/s, but only one frame per panel refresh
      (300 Hz there) is displayed.

## Open questions

None at present. All questions raised during the M1 review have been
answered and recorded under [Host-side decisions](#host-side-decisions).
