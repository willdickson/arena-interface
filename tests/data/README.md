# Test data

`js_*.pat` are reference pattern files written by the JavaScript encoder in
`reference/js/pat-encoder.js`. `tests/test_pattern.py` checks that the Python
encoder produces byte-identical files from the same pixel values.

Regenerate (needs Node.js) from the repository root:

```bash
node tests/data/make_js_reference.js "$PWD"
```
