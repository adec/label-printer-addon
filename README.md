# Label Printer — Home Assistant add-on repository

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMaxGramser%2Flabel-printer-addon)

A generic **label print service** for USB label printers in Home Assistant.
Send it a finished **PNG/PDF** — or **raw ZPL** — over HTTP and it prints via
CUPS. It renders nothing itself, so *anything* that can produce a label can
print with it.

- 🔌 **Plugged in + recognized = available** — auto-detects DYMO LabelWriter
  (300/400/450/550/4XL/5XL) and Zebra ZPL/EPL printers side by side, no
  per-printer switches.
- 🏷️ **Pick your labels the way you buy them** — by DYMO part number
  (99014, 99010, 11354, …) or Zebra size (104×159 PostNL, 102×152 4×6", …),
  with a custom-size escape hatch.
- 🌐 **Simple HTTP API** — `GET /printers` tells clients exactly what to
  render (`native_px` per loaded label); `POST /print` prints it. Raw ZPL
  passes through untouched (barcodes stay pixel-exact).

## Install

1. Click the badge above (or add
   `https://github.com/MaxGramser/label-printer-addon` under **Settings →
   Add-ons → Add-on store → ⋮ → Repositories**).
2. Install **Label Printer** — the first build compiles the DYMO driver and
   takes a few minutes.
3. Plug in your printer(s), set the loaded label in **Configuration**, start.

Full documentation: [label_printer/DOCS.md](label_printer/DOCS.md).

## Used by

- [**Fridge Assistant**](https://github.com/MaxGramser/fridge_assistant) —
  fridge/freezer inventory with printed stickers.
- [**Label Assistant**](https://github.com/MaxGramser/label_assistant) —
  a kid-friendly Canva-style label editor.

Both work fine without a printer; install this add-on to make them print.

## License

MIT
