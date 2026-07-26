# 🖨️ Label Printer

Prints **PNG/PDF** labels — and **raw ZPL** — on USB label printers via CUPS.
Auto-detects DYMO LabelWriter and Zebra printers side by side (plugged in +
recognized = available); you only tell it **which label roll is loaded**, by
DYMO part number or Zebra size.

Built as the print engine for
[Fridge Assistant](https://github.com/MaxGramser/fridge_assistant) and
[Label Assistant](https://github.com/MaxGramser/label_assistant), usable as a
generic label print service for anything (an automation, a webshop).

**Tested with a DYMO LabelWriter 400 + 99014 labels (54 × 101 mm) and a Zebra
ZD220D + 104 × 159 mm shipping labels, on one USB hub.**

See [DOCS.md](DOCS.md) for label selection, options and the HTTP API.
