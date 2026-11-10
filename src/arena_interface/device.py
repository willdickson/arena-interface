"""High-level interface to a G6 arena controller.

```python
from arena_interface import Device, TrialMode

with Device.open() as dev:                # auto-detect
    print(dev.firmware_version)
    dev.start_trial(1, TrialMode.OPEN_LOOP, frame_rate=60, duration=5)
```

Thread safety: a ``Device`` may be shared between threads. Each method is
atomic (the wire protocol allows only one request at a time and the device
serialises them), and multi-step methods such as
[`PatternLibrary.upload`][arena_interface.device.PatternLibrary.upload] hold a
lock for their whole sequence. Sequences of calls made by your own code are
not atomic; use your own lock if another thread must not interleave.
"""

from __future__ import annotations

import logging
import threading
import time
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypeVar

from . import messages as m
from ._crc import crc32
from .arena import PRESETS, Arena
from .discovery import PortInfo, find_ports
from .errors import (
    CommandError,
    DeviceDisconnected,
    DeviceError,
    DeviceNotFound,
    PerformanceWarning,
    ProtocolError,
    UnsupportedCommand,
)
from .framing import Response
from .messages import (
    AnalogInputs,
    ControllerInfo,
    CrashReport,
    DioPort,
    FirmwareImageInfo,
    FirmwareVersion,
    FramePosition,
    Health,
    PatternInfo,
    Request,
    SdInfo,
)
from .pattern import DEFAULT_DUTY_CYCLE, Origin, Pattern
from .playback import LatePolicy, Pacer, Playback, PlaybackReport, run_sequence
from .protocol import (
    AoLutMode,
    AoMode,
    Capability,
    DioRole,
    FirmwareFlag,
    GrayLevel,
    PanelDisplayMode,
    TrialMode,
    stream_frame_size,
)
from .streaming import Sampler, Telemetry
from .telemetry import FrameRecord
from .testing import FakeController, FakePort
from .transport import Transport

__all__ = [
    "Device",
    "Diagnostics",
    "IO",
    "PanelFirmware",
    "PanelCheck",
    "PatternEntry",
    "PatternLibrary",
]

_log = logging.getLogger(__name__)

#: Panels redraw their LEDs at this rate (panel firmware); faster refresh is wasted.
PANEL_SCAN_HZ = 1000
#: GS16 refresh rate at which a 2x10 arena's SPI link saturated (measured, 25 MHz).
GS16_REFRESH_LIMIT_2X10 = 1280
#: Warn when fps exceeds this fraction of the refresh rate.
FPS_REFRESH_MARGIN = 0.9
_T = TypeVar("_T")

#: Reply timeout (s) used when probing candidate ports.
PROBE_TIMEOUT = 0.5
#: Reply timeout (s) for the ALL_OFF sent while closing.
CLOSE_TIMEOUT = 0.5


def _describe(requirement: Capability | FirmwareFlag) -> str:
    kind = "capability" if isinstance(requirement, Capability) else "firmware flag"
    return f"{kind} {requirement.name}"


class Device:
    """A connection to one G6 arena controller.

    Create it with [`Device.open`][arena_interface.device.Device.open] and use
    it as a context manager, or call
    [`close`][arena_interface.device.Device.close] yourself. Closing blanks the
    arena (ALL_OFF) unless ``all_off_on_close`` is False.

    Feature groups are attributes: ``patterns`` (SD card), ``io`` (analog and
    digital BNCs), ``telemetry`` (event log), ``diagnostics`` and
    ``panel_firmware``.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        all_off_on_close: bool = True,
        arena_shape: tuple[int, int] | None = None,
        simulator: FakeController | None = None,
    ) -> None:
        """Wrap an open transport; most code should use ``Device.open``.

        Args:
            transport: Connected transport.
            all_off_on_close: Send ALL_OFF when closing.
            arena_shape: Panel rows and columns of the firmware build, for
                firmware that cannot report them.
            simulator: The simulated controller, when ``transport`` is a fake.

        Raises:
            ProtocolError: If the device does not identify as a G6 controller.
        """
        self._transport = transport
        self._lock = threading.RLock()
        self.all_off_on_close = all_off_on_close
        self._simulator = simulator
        self._port_name = transport.name
        self._user_shape = arena_shape
        self._frame_count: int | None = None
        self.patterns = PatternLibrary(self)
        """SD-card pattern library."""
        self.io = IO(self)
        """Analog and digital I/O."""
        self.diagnostics = Diagnostics(self)
        """Health and bench diagnostics."""
        self.telemetry = Telemetry(self)
        """The controller's event log (commands, frame changes, state changes)."""
        self.panel_firmware = PanelFirmware(self)
        """Panel firmware update."""
        self._identify()

    # -- opening and closing ----------------------------------------------

    @classmethod
    def open(
        cls,
        port: str | None = None,
        *,
        simulate: bool | FakeController = False,
        probe: bool = True,
        all_off_on_close: bool = True,
        arena_shape: tuple[int, int] | None = None,
        cache_patterns: bool = False,
    ) -> Device:
        """Connect to a controller.

        Args:
            port: Serial port name. If omitted, the controller is found by its
                USB ids (see [`find_ports`][arena_interface.discovery.find_ports]).
            simulate: Connect to a simulated controller instead of hardware:
                True for a new default one, or a
                [`FakeController`][arena_interface.testing.FakeController].
            probe: When several candidate ports are found, ask each one for its
                controller info to pick the controller. Without probing, more
                than one candidate is an error.
            all_off_on_close: Blank the arena when the connection is closed.
            arena_shape: ``(rows, cols)`` of the firmware build, for firmware
                too old to report it.
            cache_patterns: Read the SD card's pattern names now and keep them,
                so choosing a pattern by name is fast (see
                [`PatternLibrary.cache`][arena_interface.device.PatternLibrary.cache]).

        Returns:
            The connected device.

        Raises:
            DeviceNotFound: If no controller is found, the choice is ambiguous,
                or the port cannot be opened.
            DeviceTimeout: If the device does not answer.
            ProtocolError: If the device does not identify as a G6 controller.

        Example:
            >>> with Device.open(simulate=True) as dev:
            ...     print(dev.arena_shape)
            (2, 10)
        """
        if simulate is not False:
            controller = (
                simulate if isinstance(simulate, FakeController) else FakeController()
            )
            dev = cls(
                Transport(FakePort(controller)),
                all_off_on_close=all_off_on_close,
                arena_shape=arena_shape,
                simulator=controller,
            )
        else:
            if port is None:
                port = cls._detect(probe)
            dev = cls(
                Transport.open(port),
                all_off_on_close=all_off_on_close,
                arena_shape=arena_shape,
            )
        if cache_patterns:
            try:
                dev.patterns.cache = True
            except BaseException:
                dev.close()
                raise
        return dev

    @staticmethod
    def _detect(probe: bool) -> str:
        candidates = find_ports()
        if not candidates:
            raise DeviceNotFound(
                "no G6 arena controller found (no USB serial port with Teensy ids "
                "16C0:0483); check the cable, or pass the port name"
            )
        if len(candidates) == 1:
            return candidates[0].device
        names = ", ".join(c.device for c in candidates)
        if not probe:
            raise DeviceNotFound(
                f"several candidate ports ({names}); pass the port name"
            )
        confirmed = [c.device for c in candidates if _is_controller(c)]
        if len(confirmed) == 1:
            return confirmed[0]
        if not confirmed:
            raise DeviceNotFound(f"none of {names} answered as a G6 arena controller")
        raise DeviceNotFound(
            f"several controllers found ({', '.join(confirmed)}); pass the port name"
        )

    def _identify(self) -> None:
        self._transport.discard_input()
        reply = self._send(m.get_controller_info())
        self.info: ControllerInfo = m.decode_controller_info(reply.payload)
        """Protocol version, capabilities and MAC address, read when connecting."""
        if Capability.G6_MODE not in self.info.capabilities:
            raise ProtocolError(f"{self._port_name} is not a G6 arena controller")
        self.firmware: FirmwareVersion | None = None
        """Firmware build identity, or None if the firmware cannot report it."""
        self._boot_time: float | None = None
        if Capability.HEALTH in self.info.capabilities:
            self.firmware = m.decode_firmware_version(
                self._send(m.get_firmware_version()).payload
            )
            # Host time of the controller's boot, to date telemetry records that
            # are older than one wrap (71.6 min) of its microsecond clock.
            t0 = time.time()
            health = m.decode_health(self._send(m.get_health()).payload)
            self._boot_time = (t0 + time.time()) / 2 - health.uptime_ms / 1000

    def close(self) -> None:
        """Blank the arena (unless disabled) and release the port.

        Safe to call more than once and after the device has disappeared.
        """
        with self._lock:
            if not self._transport.is_open:
                return
            if self.all_off_on_close:
                try:
                    req = m.all_off()
                    self._transport.request(
                        req.frame, expect=req.opcode, timeout=CLOSE_TIMEOUT
                    )
                except DeviceError as exc:
                    _log.debug("ALL_OFF on close failed: %s", exc)
            self._transport.close()

    def __enter__(self) -> Device:
        """Return self."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the device (blanking the arena unless disabled)."""
        self.close()

    def __repr__(self) -> str:
        """Short description including the port."""
        state = "open" if self.is_open else "closed"
        return f"<Device {self._port_name} {state}>"

    @property
    def is_open(self) -> bool:
        """True while connected."""
        return self._transport.is_open

    @property
    def port_name(self) -> str:
        """Name of the serial port (``fake://arena`` when simulated)."""
        return self._port_name

    @property
    def simulator(self) -> FakeController | None:
        """The simulated controller, or None when connected to hardware."""
        return self._simulator

    def reset(self) -> None:
        """Reboot the controller (SYSTEM_RESET).

        The USB connection drops, so the device is closed afterwards; call
        [`reconnect`][arena_interface.device.Device.reconnect] to continue.
        """
        with self._lock:
            self._send(m.system_reset())
            self._frame_count = None
            self._transport.close()

    def reconnect(self, timeout: float = 10.0, poll_interval: float = 0.25) -> None:
        """Re-open the same controller after a reset or an unplug.

        Polls the port until the controller answers again. If the MAC address
        was known, it must match, so a different controller on the same port
        is not picked up by mistake.

        Args:
            timeout: Seconds to keep trying.
            poll_interval: Seconds between attempts.

        Raises:
            DeviceNotFound: If the controller does not come back in time, or a
                different controller answers.
        """
        with self._lock:
            self._transport.close()
            old_mac = self.info.mac
            deadline = time.monotonic() + timeout
            last_error: Exception | None = None
            while True:
                try:
                    if self._simulator is not None:
                        self._transport = Transport(FakePort(self._simulator))
                    else:
                        self._transport = Transport.open(self._port_name)
                    self._identify()
                    break
                except DeviceError as exc:
                    last_error = exc
                    self._transport.close()
                if time.monotonic() >= deadline:
                    raise DeviceNotFound(
                        f"{self._port_name} did not come back within {timeout:g} s"
                        + (f" ({last_error})" if last_error else "")
                    )
                time.sleep(poll_interval)
            self._frame_count = None
            if old_mac and self.info.mac and self.info.mac != old_mac:
                self._transport.close()
                raise DeviceNotFound(
                    f"a different controller ({self.info.mac}) answered on "
                    f"{self._port_name}; expected {old_mac}"
                )
            self.patterns.refresh()  # the card may have changed meanwhile

    # -- identity ---------------------------------------------------------

    @property
    def firmware_version(self) -> str:
        """One-line firmware description, e.g. ``"06a6f25 2x10 2026-09-12 main"``."""
        return (
            str(self.firmware)
            if self.firmware
            else "unknown (firmware too old to report)"
        )

    @property
    def capabilities(self) -> Capability:
        """Capability bits advertised by the controller."""
        return self.info.capabilities

    @property
    def firmware_flags(self) -> FirmwareFlag:
        """Firmware feature flags (empty if the firmware cannot report them)."""
        return self.firmware.flags if self.firmware else FirmwareFlag(0)

    @property
    def arena_shape(self) -> tuple[int, int] | None:
        """``(rows, cols)`` of panels the firmware drives, if known."""
        if self.firmware:
            return self.firmware.rows, self.firmware.cols
        return self._user_shape

    @property
    def panel_count(self) -> int | None:
        """Number of panels the firmware drives, if known."""
        shape = self.arena_shape
        return shape[0] * shape[1] if shape else None

    @property
    def arena(self) -> Arena:
        """The full panel grid the controller drives, as an ``Arena``.

        Use it to size images and patterns for this controller. It is the
        matching full-grid preset when there is one (e.g. ``G6_2x10``, whose id
        is then written to pattern headers), otherwise a plain ``Arena``.
        Panels removed from the arena do not change it.

        Raises:
            DeviceError: If the firmware does not report its grid and no
                ``arena_shape`` was given to ``Device.open()``.

        Example:
            >>> with Device.open(simulate=True) as dev:
            ...     print(dev.arena.name, dev.arena.pixel_shape)
            G6_2x10 (40, 200)
        """
        shape = self.arena_shape
        if shape is None:
            raise DeviceError(
                "this firmware does not report its panel grid; pass arena_shape "
                "to Device.open()"
            )
        for preset in PRESETS.values():
            if (preset.rows, preset.cols) == shape and not preset.is_partial:
                return preset
        return Arena(*shape)

    def supports(self, requirement: Capability | FirmwareFlag) -> bool:
        """True if the controller advertises a capability or firmware flag."""
        if isinstance(requirement, Capability):
            return requirement in self.capabilities
        return requirement in self.firmware_flags

    # -- display ----------------------------------------------------------

    def all_off(self) -> None:
        """Blank the arena and stop any display."""
        self._send(m.all_off())

    def all_on(self) -> None:
        """Light every pixel at full brightness."""
        self._send(m.all_on())

    def stop(self) -> None:
        """Stop the display and blank the arena."""
        self._send(m.stop_display())

    def start_trial(
        self,
        pattern: int | str,
        mode: TrialMode = TrialMode.OPEN_LOOP,
        *,
        frame_rate: int = 0,
        initial_frame: int = 0,
        gain: int = 0,
        duration: float = 0.0,
        duty_cycle: int | None = None,
    ) -> None:
        """Start displaying an SD-card pattern.

        Args:
            pattern: The SD-card pattern: its file name (``.pat`` optional,
                case ignored) or its id, its number in the alphabetical list
                of file names starting at 1 (see ``device.patterns``). Ids
                change when patterns are added, renamed or deleted; names do
                not.
            mode: Open loop (auto-advance), show frame (you choose the frame)
                or closed loop (frame rate follows Analog In 1).
            frame_rate: Frames per second in open loop; negative plays backwards.
            initial_frame: Frame to start on (the first frame is 0).
            gain: Closed-loop gain in tenths of frames per second per volt.
            duration: Seconds until the controller stops by itself; 0 = never.
            duty_cycle: Brightness 1-255 for this trial, or None for the
                pattern's own.

        Raises:
            KeyError: If no pattern has that name.
            CommandError: If the pattern cannot be loaded (the arena shows an
                error code briefly and goes dark).

        Example:
            >>> with Device.open(simulate=True) as dev:
            ...     dev.start_trial("demo_gs2", frame_rate=30)  # demo_gs2.pat
            ...     dev.start_trial(1, frame_rate=30)  # the first pattern
        """
        with self._lock:
            pattern_id = self._pattern_id(pattern)
            self._frame_count = None
            self._send(
                m.trial_params(
                    mode,
                    pattern_id,
                    frame_rate=frame_rate,
                    initial_frame=initial_frame,
                    gain=gain,
                    duration=duration,
                    duty_cycle=duty_cycle,
                )
            )
            self._frame_count = self.frame_position.frame_count

    def show_pattern(self, pattern: int | str) -> None:
        """Load a pattern and show its first frame, without advancing.

        Args:
            pattern: File name (``.pat`` optional, case ignored) or id (the
                first pattern is 1), as for ``start_trial``.

        Raises:
            KeyError: If no pattern has that name.
        """
        with self._lock:
            pattern_id = self._pattern_id(pattern)
            self._frame_count = None
            self._send(m.set_pattern_id(pattern_id))
            self._frame_count = self.frame_position.frame_count

    def _pattern_id(self, pattern: int | str) -> int:
        """The id (first pattern = 1) of a pattern given by id or file name."""
        if isinstance(pattern, str):
            found = self.patterns.find(pattern)
            if found is None:
                names = [e.name for e in self.patterns]
                shown = ", ".join(names[:20]) + (", ..." if len(names) > 20 else "")
                raise KeyError(
                    f"no pattern named {pattern!r} on the SD card"
                    + (f"; patterns: {shown}" if names else " (it has no patterns)")
                )
            return found
        return pattern

    def show_frame(self, index: int) -> None:
        """Show one frame of the loaded pattern (the first frame is 0).

        Raises:
            ValueError: If the index is beyond the loaded pattern (checked
                locally, because the controller would stop the display).
            CommandError: If no pattern is loaded.
        """
        count = self._frame_count
        if count is not None and not 0 <= index < count:
            raise ValueError(f"frame index must be 0..{count - 1}, got {index}")
        self._send(m.set_frame_position(index))

    @property
    def frame_position(self) -> FramePosition:
        """Current frame index and frame count of the loaded pattern."""
        return m.decode_frame_position(self._send(m.get_frame_position()).payload)

    def stream_frame(self, payload: bytes) -> None:
        """Display a frame sent from the host.

        Args:
            payload: One frame: ``"FR"``, a frame index (u16) and one panel
                block per panel. Its size selects GS2 or GS16.

        Raises:
            ValueError: If the size does not match the arena.
        """
        panels = self.panel_count
        if panels is not None:
            sizes = {stream_frame_size(panels, g) for g in GrayLevel}
            if len(payload) not in sizes:
                raise ValueError(
                    f"a frame for {panels} panels is {sorted(sizes)} bytes, "
                    f"got {len(payload)}"
                )
        self._send(m.stream_frame(payload))

    def stream_image(
        self,
        image: Any,
        *,
        gray_level: GrayLevel = GrayLevel.GS16,
        origin: Origin = "upper",
        duty_cycle: int = DEFAULT_DUTY_CYCLE,
        arena: Arena | None = None,
    ) -> None:
        """Display one image sent from the host.

        Args:
            image: Pixel rows (nested lists or a 2-D numpy array) covering the
                whole arena: 0/1 for GS2, 0-15 for GS16.
            gray_level: GS2 or GS16.
            origin: ``"upper"`` if row 0 is the top of the arena, ``"lower"``
                if it is the bottom.
            duty_cycle: Brightness 1-255.
            arena: The rig's arena, if it differs from the controller's grid;
                the image is then fitted to the grid. If omitted, the image
                size gives the arena.

        Raises:
            ValueError: If the image does not match or fit the controller's grid.
        """
        frame = Pattern.from_frames(
            [image],
            gray_level=gray_level,
            arena=arena,
            origin=origin,
            duty_cycle=duty_cycle,
        )
        shape = self.arena_shape
        if shape is not None:
            frame = frame.fit_to(*shape)
        elif frame.arena.panel_count != 20:
            raise ValueError(
                "this firmware does not report its panel grid; pass arena_shape to "
                "Device.open() to stream images that are not 20 panels"
            )
        self._send(m.stream_frame(frame.stream_payload(0)))

    def show_panel_frame(self, index: int) -> None:
        """Show a frame stored in the panels' own memory (PSRAM)."""
        self._send(m.display_psram_index(index))

    def play_panel_frames(self, start: int, count: int, fps: int) -> None:
        """Play frames ``start .. start+count-1`` from the panels' own memory."""
        self._send(m.psram_play(start, count, fps))

    @property
    def refresh_rate(self) -> int:
        """Rate (Hz) at which the current frame is re-sent to the panels.

        Once set, it stays in force until the controller restarts.
        """
        return m.decode_u16(self._send(m.get_refresh_rate()).payload)

    @refresh_rate.setter
    def refresh_rate(self, hz: int) -> None:
        if hz > PANEL_SCAN_HZ:
            warnings.warn(
                f"refresh rate {hz} Hz is above the panels' {PANEL_SCAN_HZ} Hz scan, "
                "so it brings no visible benefit, and GS16 frames saturate the SPI "
                f"link near {self._gs16_refresh_limit()} Hz on this arena",
                PerformanceWarning,
                stacklevel=2,
            )
        self._send(m.set_refresh_rate(hz))

    @property
    def panel_display_mode(self) -> PanelDisplayMode:
        """How panels present frames: oneshot, persist, triggered or gated."""
        return PanelDisplayMode(
            m.decode_u8(self._send(m.get_panel_display_mode()).payload)
        )

    @panel_display_mode.setter
    def panel_display_mode(self, mode: PanelDisplayMode) -> None:
        self._send(m.set_panel_display_mode(mode))

    @property
    def frames_sent(self) -> int:
        """Frames pushed to the panels since boot or the last reset of the counter."""
        return m.decode_u32(self._send(m.get_frames_sent()).payload)

    def reset_frames_sent(self) -> None:
        """Zero the frames-sent counter."""
        self._send(m.reset_frames_sent())

    # -- host-paced playback ---------------------------------------------

    def play_frames(
        self,
        frames: Iterable[int],
        fps: float,
        *,
        pattern: int | str | None = None,
        late: LatePolicy = "skip",
        refresh_rate: int | None = None,
        verify: bool = False,
    ) -> PlaybackReport:
        """Show frames of the loaded SD pattern at a fixed rate, paced by the computer.

        Args:
            frames: Frame numbers (the first frame is 0), in the order to show
                them.
            fps: Frames per second.
            pattern: Load this pattern first (file name or id, as for
                ``show_pattern``); by default the pattern already loaded is
                used.
            late: ``"skip"`` drops frames whose time has passed, keeping the
                schedule; ``"send"`` sends every frame, possibly late.
            refresh_rate: Set the controller's refresh rate first. It stays in
                force until the controller restarts (see ``refresh_rate``).
            verify: Afterwards, read the controller's log to count the frame
                changes actually displayed (needs telemetry); the report's
                ``displayed`` then equals ``changes`` if none was lost.

        Returns:
            What happened: a
            [`PlaybackReport`][arena_interface.playback.PlaybackReport].

        Raises:
            ValueError: If no pattern is loaded or an index is out of range.

        Example:
            >>> with Device.open(simulate=True) as dev:
            ...     report = dev.play_frames(range(16), fps=100, pattern=1)
            >>> report.sent
            16
        """
        job = self._frames_job(frames, fps, pattern, late, refresh_rate, verify)
        return job[0](threading.Event(), None)

    def start_frames(
        self,
        frames: Iterable[int],
        fps: float,
        *,
        pattern: int | str | None = None,
        late: LatePolicy = "skip",
        refresh_rate: int | None = None,
        verify: bool = False,
    ) -> Playback:
        """Like ``play_frames``, but in a background thread.

        Returns:
            A running [`Playback`][arena_interface.playback.Playback]; call
            ``wait()`` for the report or ``stop()`` to end early.
        """
        run, total = self._frames_job(frames, fps, pattern, late, refresh_rate, verify)
        return Playback(run, total)

    def play_images(
        self,
        images: Pattern | Sequence[Any],
        fps: float,
        *,
        gray_level: GrayLevel = GrayLevel.GS16,
        origin: Origin = "upper",
        arena: Arena | None = None,
        duty_cycle: int = DEFAULT_DUTY_CYCLE,
        late: LatePolicy = "skip",
        refresh_rate: int | None = None,
    ) -> PlaybackReport:
        """Stream images to the arena at a fixed rate, paced by the computer.

        All frames are encoded before playback starts, so encoding does not
        affect the timing. The first frame is sent once while preparing, to
        put the controller in the right refresh rate for the gray level.

        Args:
            images: A [`Pattern`][arena_interface.pattern.Pattern], or images
                as for ``Pattern.from_frames`` (nested lists or numpy arrays).
            fps: Frames per second.
            gray_level: GS2 or GS16 (ignored for a ``Pattern``).
            origin: ``"upper"`` or ``"lower"`` (ignored for a ``Pattern``).
            arena: The rig's arena, if the images are drawn for a rig other
                than the controller's grid (ignored for a ``Pattern``).
            duty_cycle: Brightness 1-255 (ignored for a ``Pattern``).
            late: ``"skip"`` or ``"send"``, as for ``play_frames``.
            refresh_rate: Set the controller's refresh rate first.

        Returns:
            What happened: a
            [`PlaybackReport`][arena_interface.playback.PlaybackReport].
        """
        job = self._images_job(
            images, fps, gray_level, origin, arena, duty_cycle, late, refresh_rate
        )
        return job[0](threading.Event(), None)

    def start_images(
        self,
        images: Pattern | Sequence[Any],
        fps: float,
        *,
        gray_level: GrayLevel = GrayLevel.GS16,
        origin: Origin = "upper",
        arena: Arena | None = None,
        duty_cycle: int = DEFAULT_DUTY_CYCLE,
        late: LatePolicy = "skip",
        refresh_rate: int | None = None,
    ) -> Playback:
        """Like ``play_images``, but in a background thread.

        Returns:
            A running [`Playback`][arena_interface.playback.Playback].
        """
        run, total = self._images_job(
            images, fps, gray_level, origin, arena, duty_cycle, late, refresh_rate
        )
        return Playback(run, total)

    def pace(
        self,
        rate_hz: float,
        *,
        duration: float | None = None,
        count: int | None = None,
        late: LatePolicy = "skip",
        refresh_rate: int | None = None,
    ) -> Pacer:
        """A fixed-rate clock for live (closed-loop) playback.

        Iterate over it and send one frame per tick, chosen as you go:

        ```python
        pacer = dev.pace(200, duration=10)
        for tick in pacer:
            dev.show_frame(choose_frame())
        print(pacer.report)
        ```

        Args:
            rate_hz: Ticks per second.
            duration: Stop after this many seconds (None: until you break).
            count: Stop after this many ticks.
            late: ``"skip"`` jumps over ticks whose time has passed (the next
                tick says how many); ``"send"`` delivers every tick, late.
            refresh_rate: Set the controller's refresh rate first.

        Returns:
            An iterable [`Pacer`][arena_interface.playback.Pacer].
        """
        if refresh_rate is not None:
            self.refresh_rate = refresh_rate
        current = self.refresh_rate
        self._warn_fps(rate_hz, current, None)
        return Pacer(
            rate_hz, duration=duration, count=count, late=late, refresh_rate=current
        )

    def _frames_job(
        self,
        frames: Iterable[int],
        fps: float,
        pattern: int | str | None,
        late: LatePolicy,
        refresh_rate: int | None,
        verify: bool,
    ) -> tuple[
        Callable[[threading.Event, Callable[[int], None] | None], PlaybackReport], int
    ]:
        """Validate and prepare frame playback; return (runner, frame count)."""
        Pacer(fps, late=late)  # validates fps and late
        if pattern is not None:
            self.show_pattern(pattern)
        count = self._frame_count
        if count is None:
            count = self.frame_position.frame_count
        if not count:
            raise ValueError(
                "no pattern is loaded; pass pattern= or call show_pattern()"
            )
        sequence = list(frames)
        bad = [i for i in sequence if not isinstance(i, int) or not 0 <= i < count]
        if bad:
            raise ValueError(f"frame indices must be 0..{count - 1}; got {bad[:5]}")
        if verify:
            self._gate(m.get_telemetry_block())
        if refresh_rate is not None:
            self.refresh_rate = refresh_rate
        refresh = self.refresh_rate
        self._warn_fps(fps, refresh, GrayLevel.GS16)
        requests = [m.set_frame_position(i) for i in sequence]

        def run(
            stop: threading.Event, progress: Callable[[int], None] | None
        ) -> PlaybackReport:
            if verify:
                showing = self.frame_position.index
                self.telemetry.drain()
            schedule, sent = run_sequence(
                len(requests),
                lambda k: self._send(requests[k]),
                fps,
                late,
                stop,
                progress,
            )
            extra: dict[str, Any] = {"refresh_rate": refresh}
            if verify:
                shown = [
                    e.record
                    for e in self.telemetry.drain()
                    if isinstance(e.record, FrameRecord)
                ]
                sent_frames = sequence[:sent] if not schedule.skipped else None
                if sent_frames is not None:
                    previous = [showing, *sent_frames[:-1]]
                    extra["changes"] = sum(
                        a != b for a, b in zip(previous, sent_frames, strict=True)
                    )
                extra["displayed"] = len(shown)
                extra["superseded"] = sum(f.superseded for f in shown)
            return schedule.report(sent, **extra)

        return run, len(requests)

    def _images_job(
        self,
        images: Pattern | Sequence[Any],
        fps: float,
        gray_level: GrayLevel,
        origin: Origin,
        arena: Arena | None,
        duty_cycle: int,
        late: LatePolicy,
        refresh_rate: int | None,
    ) -> tuple[
        Callable[[threading.Event, Callable[[int], None] | None], PlaybackReport], int
    ]:
        """Validate, encode and prepare image playback; return (runner, frame count)."""
        Pacer(fps, late=late)  # validates fps and late
        pattern = (
            images
            if isinstance(images, Pattern)
            else Pattern.from_frames(
                images,
                gray_level=gray_level,
                arena=arena,
                origin=origin,
                duty_cycle=duty_cycle,
            )
        )
        shape = self.arena_shape
        if shape is not None:
            pattern = pattern.fit_to(*shape)
        elif pattern.arena.panel_count != 20:
            raise ValueError(
                "this firmware does not report its panel grid; pass arena_shape to "
                "Device.open() to stream images that are not 20 panels"
            )
        requests = [
            m.stream_frame(pattern.stream_payload(i)) for i in range(len(pattern))
        ]
        if refresh_rate is not None:
            self.refresh_rate = refresh_rate
        self._send(requests[0])  # sets the refresh rate for this gray level
        refresh = self.refresh_rate
        self._warn_fps(fps, refresh, pattern.gray_level)

        def run(
            stop: threading.Event, progress: Callable[[int], None] | None
        ) -> PlaybackReport:
            schedule, sent = run_sequence(
                len(requests),
                lambda k: self._send(requests[k]),
                fps,
                late,
                stop,
                progress,
            )
            return schedule.report(sent, refresh_rate=refresh)

        return run, len(requests)

    def _gs16_refresh_limit(self) -> int:
        """Estimated GS16 refresh rate at which SPI saturates (measured on 2x10)."""
        panels = self.panel_count or 20
        return round(GS16_REFRESH_LIMIT_2X10 * 20 / panels)

    def _warn_fps(self, fps: float, refresh: int, gray: GrayLevel | None) -> None:
        if fps > FPS_REFRESH_MARGIN * refresh:
            warnings.warn(
                f"{fps:g} frames/s is too close to (or above) the controller's refresh "
                f"rate of {refresh} Hz: frames can be replaced before they are "
                "displayed. Lower fps or raise refresh_rate.",
                PerformanceWarning,
                stacklevel=4,
            )
        limit = self._gs16_refresh_limit()
        if gray == GrayLevel.GS16 and refresh > 0.8 * limit:
            warnings.warn(
                f"a {refresh} Hz refresh rate is close to where GS16 frames saturate "
                f"the SPI link on this arena (about {limit} Hz); command handling "
                "slows down sharply there",
                PerformanceWarning,
                stacklevel=4,
            )

    # -- periodic reads ---------------------------------------------------

    def sample(
        self,
        read: Callable[[], _T],
        rate_hz: float,
        *,
        duration: float | None = None,
        count: int | None = None,
    ) -> Sampler[_T]:
        """Call ``read`` at a fixed rate and yield timestamped results.

        Args:
            read: Any function without arguments, typically a read method such
                as ``device.io.read_analog_in`` or ``lambda: device.frame_position``.
            rate_hz: Readings per second. Each reading is one USB round trip
                (about 1 ms), so a few hundred Hz is the practical limit.
            duration: Stop after this many seconds.
            count: Stop after this many samples.

        Returns:
            An iterable [`Sampler`][arena_interface.streaming.Sampler]; its
            ``skipped`` attribute counts readings dropped because one was late.

        Example:
            >>> with Device.open(simulate=True) as dev:
            ...     samples = list(dev.sample(dev.io.read_digital, 50, count=3))
            >>> len(samples)
            3
        """
        return Sampler(read, rate_hz, duration, count)

    # -- plumbing ---------------------------------------------------------

    def _gate(self, req: Request) -> None:
        if req.requires is not None and not self.supports(req.requires):
            raise UnsupportedCommand(
                f"{req.opcode.name} needs {_describe(req.requires)}, which this "
                f"firmware ({self.firmware_version}) does not advertise"
            )

    def _send(self, req: Request, *, check: bool = True) -> Response:
        self._gate(req)
        with self._lock:
            if not self._transport.is_open:
                raise DeviceDisconnected(f"{self._port_name} is closed")
            if req.exchange is m.Exchange.BULK_WRITE:
                reply = self._transport.request_bulk_write(
                    req.frame, req.data, expect=req.opcode, timeout=req.timeout
                )
            else:
                reply = self._transport.request(
                    req.frame, expect=req.opcode, timeout=req.timeout
                )
        return m.check(reply) if check else reply

    def _bulk_read(self, req: Request) -> bytes:
        self._gate(req)
        with self._lock:
            header, data = self._transport.request_bulk_read(
                req.frame, expect=req.opcode, timeout=req.timeout
            )
        m.check(header)
        return data


def _is_controller(port: PortInfo) -> bool:
    try:
        with Transport.open(port.device) as link:
            link.discard_input()
            req = m.get_controller_info()
            reply = link.request(req.frame, expect=req.opcode, timeout=PROBE_TIMEOUT)
            info = m.decode_controller_info(m.check(reply).payload)
    except DeviceError as exc:
        _log.debug("probe of %s failed: %s", port.device, exc)
        return False
    return Capability.G6_MODE in info.capabilities


# -- feature groups ----------------------------------------------------------


@dataclass(frozen=True)
class PatternEntry:
    """A pattern on the SD card.

    Attributes:
        pattern_id: Number in the alphabetical list of patterns, starting
            at 1 (it changes when patterns are added, renamed or deleted).
        name: File name.
    """

    pattern_id: int
    name: str


class PatternLibrary:
    """Patterns stored on the controller's SD card (``device.patterns``).

    Patterns are addressed by an id: their number in the alphabetical list
    of file names, starting at 1 for the first. Uploading, renaming and
    deleting re-sort the list, so ids can change; the methods that change the
    library return the new id.

    Downloading, uploading, formatting and archiving need the display to be
    off (call ``device.stop()`` first); otherwise the controller refuses with a
    [`CommandError`][arena_interface.errors.CommandError].

    Looking a pattern up by name reads every name from the card, one request
    per pattern. Turn on [`cache`][arena_interface.device.PatternLibrary.cache]
    to keep the names instead.
    """

    def __init__(self, device: Device) -> None:
        """Bind to a device."""
        self._dev = device
        self._names: list[str] | None = None  # cached names in id order

    @property
    def cache(self) -> bool:
        """Keep the pattern names, so name lookups are fast (default off).

        Setting it to True reads the names from the card at once; setting it
        to False drops them, and every lookup reads the card again.

        While it is on:

        * a name lookup (``find`` and the ``pattern`` argument of
          ``start_trial`` and friends) takes one request instead of one per
          pattern on the card: the card's name at the cached id is checked,
          so a stale cache never selects the wrong pattern. If the check
          fails, or the name is not cached, the names are read again before
          giving up;
        * ``upload``, ``rename``, ``delete`` and ``format_card`` re-read the
          names, as does ``Device.reconnect``;
        * listing the patterns (``len``, iteration, ``entries``) still reads
          the card.

        Example:
            >>> with Device.open(simulate=True) as dev:
            ...     dev.patterns.cache = True       # reads the names now
            ...     dev.start_trial("demo_gs2", frame_rate=30)
        """
        return self._names is not None

    @cache.setter
    def cache(self, enabled: bool) -> None:
        with self._dev._lock:
            if not enabled:
                self._names = None
            elif self._names is None:
                self._names = self._read_names()

    def refresh(self) -> None:
        """Re-read the cached names from the card (nothing to do if the cache is off).

        Only needed if the card was changed by something other than this
        ``Device`` and you want the next lookup to skip the check.
        """
        with self._dev._lock:
            if self._names is not None:
                self._names = self._read_names()

    def _read_names(self) -> list[str]:
        return [entry.name for entry in self.entries()]

    def __len__(self) -> int:
        """Number of patterns on the card."""
        return m.decode_u16(self._dev._send(m.get_file_count()).payload)

    def __iter__(self) -> Iterator[PatternEntry]:
        """Iterate over a snapshot of the patterns, in id order."""
        return iter(self.entries())

    def entries(self) -> list[PatternEntry]:
        """All patterns, in id order."""
        with self._dev._lock:
            return [PatternEntry(i, self.name(i)) for i in range(1, len(self) + 1)]

    def name(self, pattern_id: int) -> str:
        """File name of a pattern."""
        return m.decode_pattern_filename(
            self._dev._send(m.get_pattern_filename(pattern_id)).payload
        )

    def find(self, name: str) -> int | None:
        """Id of the pattern with this file name, or None.

        The match ignores case (as the SD card's file system does), and the
        ``.pat`` extension may be left out: ``"Grating"`` finds
        ``grating.pat``. Each call reads the names from the controller, one
        request per pattern.
        """
        wanted = name.strip().lower()
        if not wanted.endswith(".pat"):
            wanted += ".pat"
        with self._dev._lock:
            if self._names is None:
                return _index_of(wanted, self._read_names())
            found = _index_of(wanted, self._names)
            if found is not None and self._name_is(found, wanted):
                return found
            self._names = self._read_names()  # stale or missing: read again
            return _index_of(wanted, self._names)

    def _name_is(self, pattern_id: int, wanted: str) -> bool:
        """Whether the card's pattern ``pattern_id`` has this (lower-case) name."""
        try:
            return self.name(pattern_id).lower() == wanted
        except CommandError:  # the id is past the end of the list
            return False

    def info(self, pattern_id: int) -> PatternInfo:
        """Header metadata of a pattern (works while the display is running)."""
        return m.decode_pattern_info(
            self._dev._send(m.get_pattern_info(pattern_id)).payload
        )

    def download(self, pattern_id: int) -> bytes:
        """Contents of a pattern file."""
        return self._dev._bulk_read(m.get_pattern_file(pattern_id))

    def upload(
        self, name: str, data: bytes | Pattern, *, overwrite: bool = False
    ) -> int:
        """Store a pattern on the card.

        Args:
            name: File name, ending in ``.pat`` (1-63 ASCII characters).
            data: A [`Pattern`][arena_interface.pattern.Pattern], or complete
                ``.pat`` file contents. A ``Pattern`` drawn for a rig smaller
                than the controller's grid is fitted to the grid first (see
                [`Pattern.fit_to`][arena_interface.pattern.Pattern.fit_to]).
            overwrite: Replace an existing pattern with the same name.

        Returns:
            The pattern's id after the upload.

        Raises:
            FileExistsError: If the name exists and ``overwrite`` is False.
            ValueError: If the name is invalid or the pattern does not match
                (or fit) the controller's panel grid.
        """
        if not name.lower().endswith(".pat"):
            raise ValueError(f"pattern file names must end in .pat, got {name!r}")
        if isinstance(data, Pattern):
            shape = self._dev.arena_shape
            data = (data.fit_to(*shape) if shape else data).to_bytes()
        self._check_geometry(data)
        with self._dev._lock:
            existing = self.find(name)
            if existing is not None:
                if not overwrite:
                    raise FileExistsError(
                        f"pattern {name!r} already exists (id {existing})"
                    )
                self._dev._send(m.set_pattern_file(existing, data))
                return existing
            self._dev._send(m.set_pattern_file(0, data))
            return self.rename(0, name)

    def rename(self, pattern_id: int, name: str) -> int:
        """Rename a pattern and return its new id.

        If the name is taken, the controller adds an ``X001_``-style prefix;
        check [`name`][arena_interface.device.PatternLibrary.name] afterwards.
        """
        with self._dev._lock:
            new_id = m.decode_u16(
                self._dev._send(m.set_pattern_filename(pattern_id, name)).payload
            )
            self.refresh()
            return new_id

    def delete(self, pattern_id: int) -> None:
        """Delete a pattern; ids of later patterns move down by one."""
        with self._dev._lock:
            self._dev._send(m.delete_pattern_file(pattern_id))
            self.refresh()

    def format_card(self) -> None:
        """Erase the whole SD card: patterns, panel firmware and manifests."""
        with self._dev._lock:
            self._dev._send(m.purge_memory())
            self.refresh()

    def archive(self) -> bytes:
        """The SD card contents as a ZIP file."""
        return self._dev._bulk_read(m.get_sd_archive())

    def _check_geometry(self, data: bytes) -> None:
        shape = self._dev.arena_shape
        if shape is None or len(data) < 10 or data[:4] != b"G6PT":
            return
        if (data[8], data[9]) != shape:
            raise ValueError(
                f"pattern is {data[8]}x{data[9]} panels but the controller drives "
                f"{shape[0]}x{shape[1]}; it would be rejected when played"
            )


def _index_of(wanted: str, names: list[str]) -> int | None:
    """Id (position + 1) of a lower-case name in a list of names, or None."""
    for i, name in enumerate(names, start=1):
        if name.lower() == wanted:
            return i
    return None


class IO:
    """Analog and digital BNC connections (``device.io``)."""

    def __init__(self, device: Device) -> None:
        """Bind to a device."""
        self._dev = device

    @property
    def analog_out(self) -> int:
        """Analog output in millivolts (0-5000), as read back from the DAC.

        Setting it stops any lookup table.
        """
        return m.decode_u16(self._dev._send(m.get_ao_voltage()).payload)

    @analog_out.setter
    def analog_out(self, mv: int) -> None:
        self._dev._send(m.set_ao_voltage(mv))

    def set_analog_out_mode(self, mode: AoMode) -> None:
        """Choose whether the analog output is programmable or tracks the frame."""
        self._dev._send(m.set_ao_mode(mode))

    def load_analog_table(
        self,
        values_mv: list[int] | tuple[int, ...],
        mode: AoLutMode = AoLutMode.FRAME_LOCKED,
        step_hz: int = 0,
    ) -> None:
        """Load and start an analog-output lookup table (1-21 levels)."""
        self._dev._send(m.set_ao_lut(values_mv, mode, step_hz))

    def read_analog_in(self) -> AnalogInputs:
        """Read both analog inputs (±10 V, uncalibrated)."""
        return m.decode_analog_in(self._dev._send(m.get_analog_in()).payload)

    def set_digital_out(self, port: int, high: bool) -> None:
        """Drive Digital IO port 1 or 2 high or low.

        A port in role OFF becomes OUT_PROGRAMMABLE; a port in an input or
        frame-scan role is refused.
        """
        self._dev._send(m.set_digital_out(port, high))

    def read_digital(self) -> tuple[bool, bool]:
        """Levels of Digital IO ports 1 and 2."""
        return m.decode_digital_out(self._dev._send(m.get_digital_out()).payload)

    def set_dio_role(self, port: int, role: DioRole) -> None:
        """Configure the role of Digital IO port 1 or 2."""
        self._dev._send(m.set_dio_role(port, role))

    def dio_roles(self) -> tuple[DioPort, DioPort]:
        """Roles and levels of Digital IO ports 1 and 2."""
        return m.decode_dio_role(self._dev._send(m.get_dio_role()).payload)


class Diagnostics:
    """Health and bench diagnostics (``device.diagnostics``)."""

    def __init__(self, device: Device) -> None:
        """Bind to a device."""
        self._dev = device

    def health(self) -> Health:
        """Controller health counters and previous-boot breadcrumbs."""
        return m.decode_health(self._dev._send(m.get_health()).payload)

    def crash_report(self) -> CrashReport:
        """The last CPU fault recorded by the controller, if any."""
        return m.decode_crash_report(self._dev._send(m.get_crash_report()).payload)

    def sd_info(self) -> SdInfo:
        """SD card identity and geometry (``mounted`` is False without a card)."""
        reply = self._dev._send(m.get_sd_info(), check=False)
        if not reply.ok and len(reply.payload) < 30:
            m.check(reply)
        return m.decode_sd_info(reply.payload)

    def set_sd_diag(
        self, *, legacy_seek: bool = False, no_same_index_skip: bool = False
    ) -> tuple[bool, bool]:
        """Set the SD bench switches and return the flags now in force."""
        reply = self._dev._send(
            m.set_sd_diag(
                legacy_seek=legacy_seek, no_same_index_skip=no_same_index_skip
            )
        )
        return m.decode_sd_diag(reply.payload)

    @property
    def debug_output(self) -> bool:
        """Debug text output on the USB link (effective on debug builds only)."""
        return bool(m.decode_u8(self._dev._send(m.get_diag_output()).payload))

    @debug_output.setter
    def debug_output(self, on: bool) -> None:
        self._dev._send(m.set_diag_output(on))

    @property
    def spi_clock_mhz(self) -> int:
        """Panel SPI clock in MHz (1-30)."""
        return m.decode_u16(self._dev._send(m.get_spi_clock()).payload)

    @spi_clock_mhz.setter
    def spi_clock_mhz(self, mhz: int) -> None:
        self._dev._send(m.set_spi_clock(mhz))

    def ip_address(self) -> str:
        """Ethernet IP address (empty while Ethernet is not running)."""
        return self._dev._send(m.get_ethernet_ip_address()).text


@dataclass(frozen=True)
class PanelCheck:
    """Result of verifying a panel's firmware.

    Attributes:
        matches: The panel runs the stored image.
        message: The controller's report.
    """

    matches: bool
    message: str


class PanelFirmware:
    """Update the panels' own firmware (``device.panel_firmware``).

    The controller keeps one panel image on its SD card; upload it, then
    program and verify panels one at a time, by panel number (the first
    panel is 1). The display must be off.
    """

    def __init__(self, device: Device) -> None:
        """Bind to a device."""
        self._dev = device

    def upload(self, image: bytes) -> int:
        """Store a panel firmware image and return its CRC-32.

        Raises:
            ProtocolError: If the controller's CRC differs from the local one.
        """
        reply = self._dev._send(m.set_firmware_file(image))
        remote = m.decode_u32(reply.payload)
        if remote != crc32(image):
            raise ProtocolError(
                f"stored image CRC 0x{remote:08x} differs from 0x{crc32(image):08x}"
            )
        return remote

    def info(self) -> FirmwareImageInfo | None:
        """Footer of the stored image, or None if no image is stored."""
        reply = self._dev._send(m.get_firmware_info(), check=False)
        if reply.status == 1 and reply.text == "No firmware image present":
            return None
        return m.decode_firmware_info(m.check(reply).payload)

    def program(self, panel_number: int) -> str:
        """Flash one panel from the stored image and return the controller's report."""
        return self._dev._send(m.program_panel(panel_number)).text

    def verify(self, panel_number: int) -> PanelCheck:
        """Check whether a panel runs the stored image."""
        reply = self._dev._send(m.verify_panel(panel_number), check=False)
        if reply.status not in (0, 1):
            m.check(reply)
        return PanelCheck(matches=reply.ok, message=reply.text)
