# Brother backend tests

```sh
python3 -m venv .venv
.venv/bin/pip install -r tests/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The suite generates real Brother raster commands for all offered label sizes,
sends bytes to a local TCP receiver, and exercises discovery, printing, self-test,
PDF page conversion (with rasterization mocked), size policies and failures.
It does not send any data to a physical printer. A Home Assistant container build
and physical PNG/PDF print test are still required for hardware validation.
