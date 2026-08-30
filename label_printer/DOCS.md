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

**On a LabelWriter 550, 550 Turbo or 5XL, leave this on `auto`** and skip the
table — the printer reads the roll itself and there is nothing to keep in
sync. See [Automatic Label Recognition](#automatic-label-recognition) below.

| Option | Size | Typical use |
|--------|------|-------------|
| `auto` | detected | LabelWriter **550 family only**: read off the roll's NFC tag *(recommended there)* |
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

### Automatic Label Recognition

Every genuine LabelWriter 550 roll carries an NFC tag, and the printer reads
it. With `dymo_label: auto` the add-on asks the printer what is loaded and
follows it: swap a roll and within about five seconds the queue's page size,
the reported canvas, and the roll gauge have all moved with it. Nothing to
configure, nothing to restart.

What that gets you, per printer, in `GET /printers`:

```json
"alr": {
  "sku": "S0722430",  "part": "99014",   "media": "w154h286",
  "labels_left": 220, "capacity": 220,
  "state": "ok",      "bay_text": "rol geladen"
}
```

`alr` is `null` on anything without label recognition, and `roll.source` tells
you which number you are looking at — `alr` (the printer counted) or
`estimate` (we counted the jobs we sent).

**A roll it cannot place** — a compatible roll with no tag, or an article
number the table does not know — is not fatal. Printing carries on at the
configured size, exactly as before, and `GET /attention` gains a
`roll_unrecognised` item so it is visible rather than silent. If you have such
a roll, set `dymo_label` to its part number and the warning goes away.

**Only the 550 family is polled** (`lw550`, `lw550t`, `lw5xl`). This is a
safety fence, not an optimisation: on a LabelWriter 450 or 400 the same
`ESC A` command is the *older* status request and answers with a single byte,
so polling one would leave a stray byte for the driver's own status read to
pick up mid-job. A 450/400/330/4XL is never opened at all.

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
| `GET` | `/printers` | Which printers exist: per printer its queue name, kind, model, connection state, **loaded label** (`loaded`, with `native_px` render size), **supported media** (`supported[]`, read from the driver), accepted formats and dpi. Every media entry also carries **`printable`** — `margin_mm` (unreachable strips at the leading/trailing/left/right edges) and `rect_px` (the reachable rectangle inside `native_px`). Includes `api_version`. |
| `GET` | `/health` | JSON status (printers connected? default media). |
| `GET` | `/journal` | The last 100 print jobs, newest first, each with a human-readable `summary` of what was done (passed through 1:1, scaled n%, cropped, rejected, failed — and why). Also shown as a table on the web UI. `?limit=N` caps the result. |
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

**Printable area:** the canvas is the full physical sticker, but the head
cannot reach all of it — DYMO LabelWriters park the label with its leading
edge past the print head (first ~5.4 mm dead, ~1-1.5 mm side tolerance, from
DYMO's own PPD `ImageableArea` per part number); Zebra desktop printers
backfeed, leaving only ~1 mm wander margin. Keep everything you care about
inside `printable.rect_px` (pixel coordinates on the `native_px` canvas,
`y = 0` is the leading edge). Full-bleed backgrounds may extend to the
canvas edge — whatever falls outside simply stays white on the sticker.

Registration is automatic: at print time the add-on drops exactly the
label's leading margin from the raster (the strip that already passed the
head), so pixel `y` lands at physical `y` with nothing to calibrate. Zebra
jobs are never shifted.

**Add-on hostname:** from another add-on or integration the API is reachable
at `http://<addon-hostname>:8000`. For this repository that hostname looks
like `http://xxxxxxxx-label-printer:8000` (shown on the add-on's page); for a
local `/addons` install it is `http://local-label-printer:8000`.

## Adding a new printer model — and teaching it "out of labels"

Getting a printer to *print* is the easy half. Getting it to *tell you it
can't* is the half that needs an afternoon with the actual hardware, because
**no two families fail the same way and none of them are honest by default**.
Do not skip this: a printer that silently swallows a label is worse than one
that refuses it, because the label looks printed to every layer above.

Work through it in this order, with the printer in front of you:

1. **Print normally first.** Get a queue, a PPD (or raw passthrough) and one
   good label. Only then start breaking things.
2. **Pull the media mid-life and print again.** Then look at all three
   layers, because they disagree:
   - `GET /debug` → `jobs` (does the job stay queued?) and the queue's
     `Alerts:` line (does CUPS raise a printer-state-reason?);
   - the add-on log;
   - the printer's own status light *and* your ears (a printer that stays
     silent has buffered your label, not printed it).
3. **If CUPS says nothing, ask the device itself.** Raw-language printers
   (ZPL/EPL) accept a status query over the same USB endpoint they take jobs
   on: `~HS` for ZPL. There is usually no `/dev/usb/lp*` node (CUPS' backend
   claims the device), so use pyusb — CUPS only holds the interface while a
   job runs, so between jobs it is free to claim. See `_zebra_host_status`.
4. **Do not trust the flag you expect.** Verify empirically which field
   actually moves, one variable at a time. Vendor docs are often paywalled or
   wrong for the specific model.
5. **Wire it into `_attention()`** with a message that names the printer in
   human words, says how many labels are waiting, and says what to *do* —
   never the raw error code (that goes in `detail`).
6. **Add a pre-flight** in `_print_bytes` for anything that can accept a job
   it cannot print, so the caller gets an honest `ok: false` instead of a
   label that exists only in RAM.
7. **Re-verify by reloading the media** and confirming `/attention` empties
   out again — a detector that never clears is an alarm nobody trusts.

### What this dance produced for the two tested models (2026-07-28)

| | DYMO LabelWriter 400 | Zebra ZD220 |
|---|---|---|
| Job while empty | stays queued, prints itself after reload | accepted into printer RAM, CUPS reports **complete** |
| CUPS signal | `Alerts: com.dymo.out-of-paper-error` (only during/after a print attempt) | **nothing, ever** |
| Device signal | — | `~HS` string 1: field `c` (pause) flips to 1; field `eee` counts buffered formats |
| Paper-out flag | n/a | field `b` stayed **0** — this model pauses instead |
| Recovery | reload roll → prints by itself | reload roll → **FEED press** needed |

The same recipe extends to other supplies: a ribbon/ink-out condition
surfaces as another `printer-state-reason` (`marker-supply-empty`,
`ribbon-out`) or another `~HS` field — add the word to `ALERT_MEANING` with
its human sentence and it flows through `/attention` unchanged.

### Getting it to Home Assistant

`GET /attention` is the endpoint to poll (`needs_attention`, `items[]`,
`message`). A REST sensor plus one automation covers it:

```yaml
rest:
  - resource: "http://local-label-printer:8000/attention"
    scan_interval: 60
    sensor:
      - name: "Labelprinter aandacht"
        value_template: "{{ 'ja' if value_json.needs_attention else 'nee' }}"
        json_attributes: [count, message, items]
```

Two details worth copying: raise a **persistent notification** (with a fixed
`notification_id`) next to the push, and dismiss it on the `to: "nee"`
transition — the alert then lives in Home Assistant until the printer really
works again, instead of scrolling away. Also trigger on `homeassistant.start`:
after a restart there is no state transition, so an existing problem would
otherwise go unmentioned.

## Troubleshooting

- **Nothing prints, log shows the job spooled** — check the printer's own
  status light; power-cycle it and restart the add-on.
- **Zebra prints across label boundaries** — recalibrate: hold the feed button
  until one flash.
- **DYMO 550 refuses to print** — see the genuine-labels note above.
- **`printer_not_connected` from the API** — the queue exists but the device
  is off/unplugged, or you asked for a printer that is not attached. `GET
  /printers` lists what is available.
- **`media_out` from the API on a Zebra** — the pre-flight `~HS` query found
  the printer paused or out of labels, so the job was refused instead of
  disappearing into printer RAM. Load a roll, close the lid, press FEED once
  (that also releases anything already buffered), then resend.
- **A label "printed" but never came out** — check `GET /attention` and `GET
  /journal`. A Zebra accepts ZPL while empty and holds it; the journal shows
  the job, `/attention` shows why nothing appeared.
