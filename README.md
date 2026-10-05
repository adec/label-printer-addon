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

## Brother QL-1110NWB over Wi-Fi or Ethernet

This fork adds a `brother` printer to the existing HTTP API. It renders
PNG/JPEG/PDF to Brother raster commands at 300 dpi and sends them directly
to the printer's TCP port (default 9100). USB DYMO/Zebra printing still uses CUPS.

Install this repository (`https://github.com/adec/label-printer-addon`) in the
Home Assistant add-on store, install or rebuild Label Printer, and set:

```yaml
brother_host: "192.168.1.50"  # your printer's IP address or DNS hostname
brother_port: 9100
brother_label: "102x152"    # MUST match the physical DK roll
brother_length_mm: 100      # only used for continuous rolls
brother_cut: true
brother_size_mismatch: "scale"
brother_crop_align: "center"
```

Leave `brother_host` empty to disable Brother printing. Reserve the printer's
IP address in DHCP, or use a stable hostname. Home Assistant must be able to
reach the printer on TCP 9100, including across VLANs if applicable. Restart
the add-on after configuration changes.

### Automatic roll detection (v0.15.0)

Set `brother_label: "auto"`, save and restart the add-on. It sends Brother's
`ESC i S` status request over TCP 9100 and reads the 32-byte reply. Supported
rectangular die-cut rolls are matched by width and length; continuous rolls
are matched by width. For continuous tape, `brother_length_mm` still sets the
cut length: the printer cannot infer the length you want or remaining tape.

`GET /printers` exposes the detected `media`, `native_px` and a `detection`
object with `width_mm`, `length_mm`, `media_type`, `no_media`, `error_bytes`
and `raw_status`. Status replies are cached for five seconds for discovery;
auto-mode print jobs always query again before conversion. Queries and print
streams are serialized. Swapping supported rolls needs no configuration change.

Auto mode refuses printing if status is unavailable, the roll is unsupported,
no media is present, or the printer reports an error. Check `detection` for
details. If your printer/network does not return status replies, select the roll
manually to retain the previously working printing path. Detection sends no
feed or cut commands. A status reply does not confirm physical completion of a
print job. Status protocol tests pass; auto detection still needs verification
on the physical printer.

Select continuous rolls by width (`12`, `29`, `38`, `50`, `54`, `62`, `102`,
`103`), or rectangular die-cut rolls by size (for example `62x100`, `102x51`,
`102x152`, `103x164`). Continuous length is 26–3000 mm. The driver cannot
encode three short die-cut sizes (`52x29`, `54x29`, `62x29`) for this model;
they are excluded. Red/black and round labels are not supported by this path.

```sh
curl http://HOME_ASSISTANT:8000/printers
curl -F printer=brother -F file=@label.png http://HOME_ASSISTANT:8000/print
curl -X POST 'http://HOME_ASSISTANT:8000/selftest?printer=brother'
```

Use `loaded.native_px` from `/printers` as the design canvas. It is the
**printable raster**, with the roll's physical margins already excluded;
`printable.rect_px` covers that canvas. `scale` fits and preserves aspect
ratio, `crop` keeps one pixel per dot and crops/pads, and `reject` returns 422
unless dimensions match. Transparent pixels become white. PDFs use Ghostscript
and print all pages. Copies are clamped to the service's existing maximum.
Only the configured or automatically detected loaded roll can be used; raw ZPL is rejected for Brother.

A successful Brother response has `ok: true`, `submitted: true`, and
`printed: false`: TCP transmission does not confirm paper movement, roll
compatibility, or physical completion. `connected` means port 9100 was reachable,
not that labels are loaded. Auto mode reports the roll dimensions and printer status bytes. History and roll counters estimate submitted jobs. If a network
send fails partway through, check the printer before retrying to avoid duplicates.

The Brother dependency is pinned to upstream commit
`9eb7b69eac9778e5a569fd896768c392e5c7c7e7` of
[brother_ql_next](https://github.com/LunarEclipse363/brother_ql_next).
This driver includes a QL-1110NWB model definition; manual printing has been confirmed on the target QL-1110NWB. The container build also needs
validation on Home Assistant; no Docker runtime was available in development.

## Install

1. Click the badge above (or add
   `https://github.com/adec/label-printer-addon` under **Settings →
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
