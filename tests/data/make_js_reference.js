// Generates reference .pat files with the JS encoder (reference/js/pat-encoder.js).
// Pixel value formulas use bottom-origin coordinates (JS convention: row 0 = bottom).
const path = require('path');
const fs = require('fs');
const PatEncoder = require(path.resolve(process.argv[2], 'reference/js/pat-encoder.js'));
const out = path.resolve(process.argv[2], 'tests/data');
function make(name, gs, rows, cols, nFrames, value, duty, arenaId) {
  const H = rows * 20, W = cols * 20;
  const frames = [];
  for (let f = 0; f < nFrames; f++) {
    const fr = new Uint8Array(H * W);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) fr[y * W + x] = value(x, y, f);
    frames.push(fr);
  }
  const buf = PatEncoder.encodeG6({ gs_val: gs, numFrames: nFrames, rowCount: rows, colCount: cols,
    pixelRows: H, pixelCols: W, frames, stretchValues: duty, arena_id: arenaId, observer_id: 0 });
  fs.writeFileSync(path.join(out, name), Buffer.from(buf));
  console.log(name, buf.byteLength);
}
make('js_gs16_2x10.pat', 16, 2, 10, 3, (x, y, f) => (x * 7 + y * 3 + f) % 16, [128, 200, 50], 1);
make('js_gs2_2x10.pat', 2, 2, 10, 2, (x, y, f) => ((x + 2 * y + f) % 3 === 0 ? 1 : 0), [255, 1], 1);
make('js_gs16_4x12.pat', 16, 4, 12, 1, (x, y, f) => (y < 20 && x < 20) ? 15 : ((x ^ y) & 15), [128], 0);
