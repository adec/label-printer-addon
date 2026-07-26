# Label Printer

A small, generic **label print service** for USB label printers. It receives a
finished **PNG/PDF** — or **raw ZPL** — over HTTP and prints it through CUPS.
It renders nothing itself, so anything that can produce a label can print with
it: [Fridge Assistant](https://github.com/MaxGramser/fridge_assistant),
[Label Assistant](https://github.com/MaxGramser/label_assistant), an
automation, or an external system such as a webshop.

> ✅ **Tested combination:** DYMO **LabelWriter 400** + **99014** labels
> (54 × 101 mm) and a **Zebra ZD220D** + 104 × 159 mm shipping labels
> (S0904980-style, for PostNL/DHL/DPD), both on one USB hub.

## How it works

```
Fridge Assistant / Label Assistant / webshop        this add-on
(renders a PNG, or generates ZPL)             →     (prints it)
    PNG · PDF · ZPL over HTTP  ─────────────────►   CUPS → USB → printer
```

**Plugged in + recognized = available.** There are no per-printer on/off
switches: every supported printer found on USB automatically gets its own CUPS
queue (`dymo`, `zebra`). Callers pick a queue by name; if they don't, the DYMO
is used — or the first available printer if there is no DYMO.

The **only** thing you configure is **which label roll is loaded** in each
printer. That setting mirrors the physical roll in the device, which is why it
lives here (in the add-on config) and nowhere else — client apps read it via
the API, they can never change it behind your back.

## Choosing the loaded label

### DYMO — pick by part number

The part number is printed on the label box. After swapping the roll: pick it
here and restart the add-on.

| Option | Size | Typical use |
|--------|------|-------------|
| `auto` | — | LabelWriter **550 family only**: the printer detects its own roll |
| `99010` | 28 × 89 mm | address |
| `99012` | 36 × 89 mm | large address |
| `99014` | 54 × 101 mm | shipping / name badge *(default)* |
| `99015` | 54 × 70 mm | multi-purpose / diskette |
| `11352` | 25 × 54 mm | return address / small |
| `11354` | 57 × 32 mm | multi-purpose |
| `99019` | 59 × 102 mm | large lever-arch file |
| `904980` | 104 × 159 mm | extra-wide shipping — **4XL/5XL only** |
| `custom` | your size | fill in **DYMO — custom label size** below |

Custom size accepts millimetres (`54x101`) or a CUPS media name (`w154h286`).

### Zebra — pick by size

Zebra rolls are sold by size rather than one canonical part number.

| Option | Typical use |
|--------|-------------|
| `104x159 mm` | shipping label (PostNL; DYMO S0904980 rolls fit too) *(default)* |
| `102x152 mm` | 4 × 6 inch shipping |
| `102x102 mm` | 4 × 4 inch |
| `100x50 mm` | product / barcode |
| `76x51 mm` | product |
| `57x32 mm` | small product |
| `50x25 mm` | tiny / barcode |
| `custom` | fill in **Zebra — custom label size** (mm, e.g. `100x150`) |

> 💡 **After changing Zebra labels**, hold the printer's **feed button** until
> it flashes once — that recalibrates the gap sensor to the new label length.

## Install

Add this repository in **Settings → Add-ons → Add-on store → ⋮ →
Repositories**:

```
https://github.com/MaxGramser/label-printer-addon
```

Then install **Label Printer** (the first build compiles the DYMO driver and
takes a few minutes), plug in + power on your printer(s), and **Start**. The
add-on **Log** should show `Printer 'dymo' ready` and/or `Printer 'zebra'
ready`.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `dymo_label` | `99014 (54 x 101 mm)` | Which roll is loaded in the DYMO, by part number (see table above). |
| `dymo_custom_media` | — | Only with `custom`: `WxH` in mm or a CUPS media name. |
| `zebra_label` | `104x159 mm (PostNL / S0904980)` | Which labels are loaded in the Zebra, by size. |
| `zebra_custom_size` | — | Only with `custom`: `WxH` in mm. |
| `printer_model` | `auto` | Advanced: force a DYMO driver (`lw550`, `lw450`, …) only if auto-detect guesses wrong. |
| `log_level` | `info` | Add-on log verbosity. |

> ⚠️ **DYMO LabelWriter 550 series:** these enforce RFID "Automatic Label
> Recognition" in firmware and will **only print genuine DYMO labels**.
> Third-party/aftermarket rolls are refused by the printer itself (nothing this
> add-on can change). Older models like the **LabelWriter 400/450 have no such
> lock** and print any compatible roll.

## HTTP API (port 8000)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/printers` | Which printers exist: per printer its queue name, kind, model, connection state, **loaded label** (`loaded`, with `native_px` render size), **supported media** (`supported[]`, read from the driver), accepted formats and dpi. Includes `api_version`. |
| `GET` | `/health` | JSON status (printers connected? default media). |
| `POST` | `/print` | Print. Multipart `file`, JSON `{image_base64\|zpl, printer, media, copies}`, or a raw body. Omit `printer` for the default queue; omit `media` to use the loaded label. |
| `POST` | `/selftest?printer=…` | Print a small built-in test label. |
| `GET` | `/` | Human-readable status page with examples. |

Examples:

```bash
# What can I print on?
curl http://homeassistant.local:8000/printers

# Print a PNG on the DYMO (default queue)
curl -F file=@label.png http://homeassistant.local:8000/print

# Print raw ZPL on the Zebra (goes to the device untouched)
curl -H 'Content-Type: application/json' \
  -d '{"printer":"zebra","zpl":"^XA^FO50,50^A0N,50,50^FDHi^FS^XZ"}' \
  http://homeassistant.local:8000/print
```

**Contract for rendering clients:** never hard-code label sizes. Read
`GET /printers`, render exactly `loaded.native_px` pixels (portrait), and POST
without a `media` field. That is how Fridge Assistant and Label Assistant
print pixel-perfect labels on whatever roll is loaded.

**Add-on hostname:** from another add-on or integration the API is reachable
at `http://<addon-hostname>:8000`. For this repository that hostname looks
like `http://xxxxxxxx-label-printer:8000` (shown on the add-on's page); for a
local `/addons` install it is `http://local-label-printer:8000`.

## Troubleshooting

- **Nothing prints, log shows the job spooled** — check the printer's own
  status light; power-cycle it and restart the add-on.
- **Zebra prints across label boundaries** — recalibrate: hold the feed button
  until one flash.
- **DYMO 550 refuses to print** — see the genuine-labels note above.
- **`printer_not_connected` from the API** — the queue exists but the device
  is off/unplugged, or you asked for a printer that is not attached. `GET
  /printers` lists what is available.
