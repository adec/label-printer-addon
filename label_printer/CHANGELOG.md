# Changelog

## 0.9.0

- **Out-of-labels detection, per hardware family.** `GET /attention` reports
  what needs a human, in plain language: which printer, how many labels are
  waiting and what to do about it (the raw CUPS/ZPL code stays in `detail`).
  Home Assistant polls this; see DOCS.md for the REST sensor + automation.
- The two families were measured, not assumed (see the new "Adding a new
  printer model" chapter in DOCS.md). A **DYMO** keeps the job queued and
  raises `com.dymo.out-of-paper-error`, and resumes on its own once the roll
  is back. A **Zebra** tells CUPS nothing at all: it accepts ZPL into printer
  RAM and reports the job complete, so the label exists nowhere until a roll
  AND a FEED press. Only the device itself knows, via a `~HS` query over USB
  (pyusb — new dependency; there is no usblp node because CUPS claims the
  device). On the ZD220 the paper-out flag stays 0 and the printer *pauses*
  instead, while the buffered-format counter rises — both are now read.
- **Zebra jobs get a pre-flight check**: a paused/empty printer returns
  `ok: false, error: media_out` with a reload hint, instead of a cheerful
  "printed" for a label that only lives in RAM.
- Also flags jams, an open lid, low media, offline queues, and any job that
  sits still for more than 25 seconds.

## 0.8.0

- **Print journal: every job records what was done to it.** Each print
  (API, selftest) gets one human-readable entry — raw ZPL passed through
  1:1 (with format/graphic counts and `^PW`/`^LL`), image scaled n% by
  fit-to-page (aspect mismatch flagged), laid on the label 1:1 with
  overhang cropped / white padded, rotated from landscape, DYMO dead-zone
  strip dropped, or rejected/failed and why. The last 100 jobs survive
  restarts (`/data/print_journal.jsonl`), are served as JSON via
  `GET /journal`, shown as a table on the web UI, and mirrored to the
  add-on log.
- **"Open web UI" button.** The add-on now declares `webui`, so the Info
  tab links straight to the status page with the print journal.

## 0.7.0

- **Manual calibration removed — registration is now automatic.** The
  `dymo/zebra_feed_offset_mm`, `dymo/zebra_left_offset_mm` and `test_print`
  options are gone, as are `GET /calibration/offsets` and
  `POST /debug/calibration`. At print time the add-on drops exactly the
  loaded label's leading margin (the same PPD `ImageableArea` data that
  feeds `printable` in `/printers`) from the raster — that strip has
  already passed the head — so pixel `y` lands at physical `y` with
  nothing to tune. Zebra jobs are never shifted (backfeed). Configure only
  which label is loaded; clients keep art inside `printable.rect_px`.

## 0.6.0

- **`GET /printers` reports the printable area per label.** Every media
  entry (`loaded` and `supported[]`, mirrored top-level as `printable`)
  now carries `printable`: `margin_mm` (leading/trailing/left/right
  unreachable strips) and `rect_px` (the reachable rectangle inside
  `native_px`). Design clients draw this as an overlay and keep art inside
  it — the label size in the config stays the full physical sticker.
- Values come from the printer's own driver: DYMO's PPD `ImageableArea`
  per part number (LabelWriters park the label with its leading edge past
  the print head, so the first ~5.4 mm plus ~1-1.5 mm at the sides are
  mechanically unreachable — verified against the LW400 Technical
  Reference and DYMO's published CUPS drivers). Zebra desktop printers
  backfeed before every label, so only a ~1 mm safety margin against
  sideways label wander remains (ZD220 spec sheet / Zebra design
  guidance). The configured feed/left calibration offsets act as floors:
  the shifted-off strip is unprintable no matter what the PPD claims.

## 0.5.0

- **Size policy per printer** for PNG/PDF/JPG jobs whose size doesn't match
  the loaded label: `dymo_size_mismatch` / `zebra_size_mismatch`, three
  modes. `scale` (default) keeps today's fit-to-page behaviour. `crop`
  never scales: the art is placed 1:1 (one image pixel = one printer dot),
  overhang falls off, alignment via `dymo_crop_align` / `zebra_crop_align`
  (`center` or `leading-edge`). `reject` refuses mismatched jobs with HTTP
  422 and a `size_mismatch` error stating the expected pixel size.
- Under crop/reject, PDFs are rasterized at head dpi with ghostscript (the
  same rasterizer CUPS uses), so their physical mm size is preserved; each
  page is compared with a 2 px tolerance for pt→px rounding, then padded or
  cropped to exact. Side effect: in these modes the feed/left offsets now
  apply to PDFs too (they already did to PNG/JPG). Multi-page PDFs print
  page for page.
- A design that is exactly the label turned sideways is rotated instead of
  cropped. Raw ZPL keeps bypassing all of this.
- `GET /printers` now reports the active policy per printer
  (`size_policy: {mode, align}`), so clients can warn before submitting.
- Policy changes take effect when the add-on (re)starts — saving the config
  in the HA UI does that for you; only direct Supervisor-API option writes
  need an explicit restart.

## 0.4.2

- The custom-size fields ("DYMO — custom label size", "Zebra — custom label
  size") are now always visible in the add-on configuration, prefilled with
  54x101 / 100x150. They used to be hidden behind "Show unused optional
  configuration options", which made picking "custom" a dead end. Still only
  used while the matching label choice is set to "custom".

## 0.4.1

- **True 1:1 printing.** `native_px` now comes from the PPD's real
  `PaperDimension` (named media, e.g. 99014 = 638×1191 px, not 642×1192 from
  the media name) and matches imagetoraster's truncating math for `Custom.*`
  sizes (104×159 mm on Zebra = 831×1271 px). Rendering at `native_px` now
  produces a raster CUPS passes through pixel-for-pixel.
- **DYMO full bleed**: at boot the per-media `ImageableArea` margins in the
  DYMO PPD are zeroed (only for media that fit the printhead). The DYMO
  filter always printed from head dot 0 and ignored those margins anyway —
  they only made `fit-to-page` shrink every label ~5–8%.
- **Zebra geometry pinned at boot**: `^XA^LS0^LH0,0^JUS^XZ` is sent to every
  Zebra queue on startup. The CUPS driver never sends `^LS`, so a Left
  Position stored in the printer by other software silently shifted every
  job sideways (clipping one edge).
- **Offset calibration in the add-on config**: per printer a feed offset and
  a sideways offset (mm, may be negative). The DYMO feed offset defaults to
  4.0 mm: LabelWriters physically start printing ~4-5 mm past the leading
  edge (the dead zone DYMO's PPDs budgeted as top margin); images are
  pre-shifted so everything lands at its true position. Plus a **test print
  on startup** option: pick a printer, restart, and a calibration label
  comes out — the play-loop for dialing offsets in. Effective values:
  `GET /calibration/offsets`.
- New diagnostics: `GET /debug/pipeline?printer=X` runs the real CUPS filter
  chain on a test image and reports the exact raster/ZPL geometry;
  `POST /debug/calibration?printer=X` prints a millimetre-ruler label (thick
  full-bleed border + rings at 1..5 mm with staircase digits) via the normal
  print path. `/debug` now also dumps the PPD geometry lines.

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
