# Changelog

## 0.13.1

- **Fixed: `/selftest` refused its own label on the DYMO.** It drew on a
  hard-coded 642 × 1192 canvas, three pixels off the 639 × 1191 this queue
  actually rasters — a leftover from before v0.4.1 measured the real raster
  size instead of deriving it from the media name. With `dymo_size_mismatch`
  on `reject` the print path threw the image straight back out
  (`size_mismatch`), so the one endpoint whose whole purpose is to prove the
  chain works could not print at all.

  The canvas now comes from `_policy_target_px()` — the same function the
  print path measures against, so the two cannot drift apart again — and the
  drawing scales to it instead of sitting at fixed pixel offsets. The label
  also prints its own raster size, which makes a future mismatch readable off
  the sticker.

## 0.13.0

- **Fixed: a print that failed while the printer was off stayed in the queue
  forever and reported the printer as jammed.** Measured 11-09-2026: a label
  was sent while the DYMO was switched off, so `raster2dymolw_v2` — which
  opens the printer itself to read its status — timed out and exited 1. CUPS
  treats a filter error as "this document is broken", so it stopped that one
  job, left the queue enabled and never retried it. Every later print went
  through fine while the dead job sat at rank 1 for nine hours.

  Two things were wrong with that. `_pending_jobs()` read `lpstat -o`, which
  prints a stopped job exactly like a waiting one, so the corpse counted as a
  label on its way out: *"De DYMO print al 521 minuten niet. Er wacht 1
  label. Labels op of vastgelopen?"* — about a printer that was idle and
  healthy. And nothing ever retried the job, even though the reason it failed
  (printer off) had long since gone away.

  Job state now comes from IPP `Get-Jobs` (`_ipp_jobs`), the only interface
  that reports `job-state` — `lpstat` has no flag for it and this build ships
  no `ipptool`. Stopped jobs are counted separately from waiting ones, and
  `_revive_stopped_jobs()` re-offers them (`lp -H restart`) as soon as the
  printer is back on the bus and free of alerts: up to 3 tries, 30s apart.
  Only after that does `/attention` mention it, and then it says what is
  actually true — the print failed and is still in the queue. A revived job's
  waiting time is measured from the retry, so a retry of an hours-old job no
  longer reads as "stuck for hours" the moment it goes pending. If cupsd
  cannot be reached over IPP the old `lpstat` reading takes over unchanged.

## 0.12.1

- **Fixed: on a restart the DYMO queue could serve the wrong geometry for the
  whole run — and print ~7% small.** `printers.json` survives in `/data`, so
  the server starts against last boot's printer list while `run.sh` is still
  registering the queues. The startup fix-up therefore ran before CUPS had
  the queue's PPD: `_normalize_dymo_ppds` opened a file that wasn't there yet
  and silently did nothing, and the printer was marked as handled and never
  revisited. Without that normalisation the imageable area keeps DYMO's stock
  margins, so `fit-to-page` scales every label down into them — the exact
  problem the normalisation exists to prevent — and `native_px` reported the
  imageable area (596 × 1109) instead of the label (639 × 1191).

  A printer is now only counted as handled once CUPS actually has its PPD;
  until then it stays "new" and is picked up on the next 5s tick. Registering
  a queue also drops any PPD sizes, imageable areas and measured rasters
  cached against the previous one.

  v0.12.0 is what exposed this: with `dymo_label: auto` the media used to be
  the literal "auto", which the warm-up skipped, so the missing normalisation
  stayed invisible.

## 0.12.0

- **New: the LabelWriter 550 now tells the add-on which roll is loaded, and
  the add-on believes it.** `dymo_label: auto` used to mean "we don't know" —
  it reported `media: auto`, `native_px: null`, and clients fell back to a
  hardcoded canvas. It now means "ask the printer", and a roll swap needs no
  configuration at all: within one 5s tick the queue's `PageSize`, the
  reported `native_px` / `printable`, and the roll gauge all follow the roll
  that is physically in the machine.

  The 550 family reads an NFC tag on every roll (DYMO calls it Automatic
  Label Recognition). The bundled DYMO driver already asks for that status on
  every page — `ESC A`, a 32-byte reply — but decodes only 11 of those bytes
  and throws the roll's identity away. We ask for the same 32 bytes and read
  the rest. The layout is from DYMO's *LabelWriter 550 Series Technical
  Reference* (2021), confirmed byte-for-byte against the real printer: bytes
  11–22 carry the article number as ASCII (`S0722430`), bytes 27–28 the
  labels left on the roll, byte 10 the bay status.

  `ESC U`, the documented 63-byte NFC dump that carries the label dimensions
  in millimetres outright, would have removed the need for any table — but
  this printer's firmware answers it with zero bytes, in both its 2- and
  3-byte forms. So a SKU→size table does that job, keyed on both the
  old-style S-codes and the newer all-numeric article numbers, since both
  turn up on real tags.

- **New: `/printers` gained an `alr` block** — `sku`, `part`, `labels_left`,
  bay `state` and its Dutch text — so a client can say *why* it thinks a
  given label is loaded. `null` on printers without label recognition.

- **Changed: the DYMO roll gauge is the printer's own count** rather than a
  tally of the jobs we sent, which drifts and forgets. `roll.source` says
  which one you are looking at (`alr` or `estimate`); the Zebra has nothing
  to ask and keeps the estimate. The estimate's capacity now also comes from
  the detected roll rather than the configured one.

- **New: an unrecognised roll speaks up** instead of silently printing at the
  configured size. `/attention` gains a `roll_unrecognised` item when the tag
  reports an article number that isn't in the table, or reports nothing at
  all (a compatible roll with no NFC tag). Printing still works and still
  falls back to the configured size, exactly as before.

  Deliberately keyed on the SKU and not on bay status 10 ("counterfeit
  media"): this LW550 raises that on genuine rolls too, which is the whole
  reason the driver's own check is patched out in the Dockerfile.

- **Only the LW550 family is ever polled** (`lw550`, `lw550t`, `lw5xl`) — the
  same line the DYMO driver draws, and it is a safety fence rather than an
  optimisation. On a LabelWriter 450 or 400, `ESC A` is the *old* status
  command and answers with a **single byte**, so polling one would leave a
  byte in the device buffer for the driver's own status read to collect
  mid-job. A 450/400/330/4XL is never opened at all. A short reply on any
  model is drained rather than left behind.

- **Fixed: the Zebra's `~HS` status query was being written into the DYMO.**
  `_usb_lp_nodes()` returned every `/dev/usb/lp*` node with no idea which
  printer was on the other end, and the query went to each in turn until one
  answered. Harmless by itself — the DYMO says nothing — but it held the
  device while doing so, and it put the DYMO into a state where its own
  status reply came back with the NFC fields blank. Nodes are now resolved
  per queue through sysfs, by the USB serial from the CUPS device URI, so
  two printers from the same maker (a 550 beside a 450) cannot be confused
  for one another. When the serial cannot single one out, the lookup reports
  nothing rather than guessing.

## 0.11.1

- **Fixed: a printer hot-plugged after the add-on started was never picked
  up — you had to restart the add-on.** v0.11.0 added a background subshell
  that rescans USB every 5s for printers not yet registered, but the loop
  died on its very first tick. `bashio` runs `run.sh` under
  `set -e -o pipefail -o errtrace`, and in `scan_dymo` / `scan_zebra` the
  line `uri="$(lpinfo -v | grep -iE 'zebra|ztc' | …)"` exits non-zero
  whenever that printer is not on USB yet (`grep` finds nothing) — which,
  under errexit+pipefail, killed the whole detection subshell. So only the
  boot-time scan ever ran: a printer present at start (typically the DYMO)
  worked, anything plugged in later stayed invisible. The detection subshell
  now runs with errexit/pipefail off (it is best-effort by design), the
  `scan_*` "nothing found" paths return 0 cleanly, and the rescan loop
  guards each call.

## 0.11.0

- **Fixed: the add-on crash-looped instead of just marking a printer
  unavailable.** Two compounding causes, both hit by a flaky USB connection
  (e.g. a printer browning out after a power outage):
  1. `config.yaml` mapped `/dev/bus/usb` as a static device on top of
     `usb: true`. That static mapping makes Supervisor's hardware monitor
     restart the whole add-on on every USB add/remove event it sees for that
     path — with a printer flapping on and off, that alone was a restart
     every few seconds. Removed the redundant mapping; `usb: true` already
     grants the same access without the restart-on-hotplug behavior.
  2. `run.sh` ran CUPS startup and USB/printer detection (`lsusb`, `lpinfo`,
     `lpadmin`) *before* starting the HTTP server, and that probing gets
     much slower against a flaky USB device (retries, resets). If it took
     too long, Supervisor's ingress/watchdog check — which expects port
     8000 to answer soon after the container starts — decided the add-on
     was unhealthy and killed it, restarting straight into the same slow
     probe again. The server now starts first; all printer detection (the
     very first scan included) runs afterwards in the background, so a slow
     or flaky USB bus can never again block the add-on from becoming ready.
- **Printers are now detected continuously, not just once at boot.** A
  printer that powers up slower than the others after an outage — or one
  that drops off USB for a moment and comes back — used to stay invisible
  until the add-on itself restarted. `run.sh` now rescans every 5s for any
  printer not yet registered (already-registered ones are left alone, so a
  working printer is never re-registered or reprinted at); `server.py`
  reloads the same file and applies its one-time geometry fixups (DYMO PPD
  margins, Zebra `^LS0^LH0,0`) only to the newly seen entries.
- **"connected" now means "plugged in right now", not "was seen once".**
  It used to reflect whether a CUPS queue existed, which stays true after a
  printer is unplugged. It now also checks the USB bus for the printer's
  vendor ID, so the dashboard and `GET /printers` correctly show "niet
  beschikbaar" for a printer that is temporarily gone instead of reporting
  it as connected.

## 0.10.0

- **The web UI now opens.** The add-on had no ingress, so "Open Web UI" sent
  the browser to `http://<lan-ip>:8000` — which never resolves from Nabu Casa
  remote, from mobile data, or from any device that cannot do mDNS, so the tab
  just span forever. The dashboard now runs through **ingress**, over Home
  Assistant's own connection and auth, and gets a sidebar panel. Port 8000
  stays published and unauthenticated: that is the print API for Fridge
  Assistant, Label Assistant and external callers, and it is unchanged.
- **A real dashboard instead of a status dump.** Per printer: a drawn
  illustration whose LED carries live status, the loaded label, the exact
  canvas to render (`native_px` @ dpi), accepted formats, today's count, and a
  one-click test print. Plus today's total, a 14-day print-volume chart with a
  table view, the filterable print journal, and the add-on's own log inline —
  so "why is there no label" no longer needs the Log tab.
- **Roll gauge (advisory).** Counts labels printed since you last pressed
  **Nieuwe rol** and holds it against the roll size (pre-filled from the DYMO
  part number: 99014 = 220, 11354 = 1000, …; editable per printer). It is a
  guess and says so — no printer reports its remaining roll. Deliberately has
  no entity and no notification: real out-of-labels detection stays
  `GET /attention`, which asks the hardware. Dashboard-only.
- Daily counters persist in `/data/print_stats.json` (60 days), so statistics
  outlive the 100-entry journal ring; seeded once from the journal on upgrade.
- The dashboard's own polling is filtered out of the access log, so `[print]`
  and `[geometry]` lines stay findable.
- New endpoints: `GET /api/state`, `GET /api/log`, `POST /api/roll`. The old
  plain status page still lives at `/old`.

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
