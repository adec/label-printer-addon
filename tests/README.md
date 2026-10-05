# Brother backend and Grocy label tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The suite generates real Brother raster commands for all offered label sizes,
sends bytes to a local TCP receiver, and exercises discovery, printing, self-test,
PDF page conversion (with rasterization mocked), size policies and failures. Detection tests use actual TCP status replies split
across writes, unsolicited notifications, cache refresh, roll swaps and errors. HTTP tests cover continuous/die-cut parsing,
source fallback, missing media and printer-not-ready handling.
It does not send any data to a physical printer. A Home Assistant container build
and physical PNG/PDF print test are still required for hardware validation.

Grocy tests decode actual QR images with ZXing, including continuous and
pre-cut layouts, and preserve the exact stock-specific payload. They exercise
JSON and PHP nested form requests, PNG previews, roll/error validation, and a
complete webhook-to-Brother-raster delivery to a loopback TCP receiver.

Enrichment tests exercise a real loopback HTTP API with the Grocy API-key
header and URL subpath, resolve product groups and stock locations, and verify
that date types and stock-entry dates remain distinct.
