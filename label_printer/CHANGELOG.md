# Changelog

## 0.4.0

- **Standalone repository** — the add-on moved out of the Fridge Assistant
  repo to `MaxGramser/label-printer-addon`.
- **Config page overhaul**: pick the loaded label by **DYMO part number**
  (99010/99012/99014/99015/11352/11354/99019/904980, `auto` roll-detect on the
  LW550 family, or a custom size) and by **Zebra size** (104x159 PostNL,
  102x152 4×6", and more, or a custom size). Clear names + explanations on the
  configuration page (English + Dutch).
- **Removed `zebra_enabled`** — plugged in + recognized = available. Every
  supported printer on USB gets a queue automatically.
- Replaced `default_media` / `zebra_label_size` with `dymo_label` /
  `zebra_label` (+ `custom` escape hatches).

## 0.3.0

- `GET /printers` now reports `api_version`, the **loaded** label and the
  **supported** media list per printer (read from the driver), and
  `custom_media` capability.
- Callers that omit a printer fall back to the first available queue when the
  default (`dymo`) is not attached — Zebra-only setups now work out of the box.

## 0.2.0

- Multi-printer: DYMO + Zebra side by side, each on its own CUPS queue.
- Raw **ZPL** passthrough for Zebra (`{"zpl": "..."}`), `/selftest`,
  `GET /printers`.

## 0.1.x

- Initial DYMO LabelWriter support: PNG/PDF over HTTP → CUPS.
