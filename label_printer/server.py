#!/usr/bin/env python3
"""Generic label print service for USB label printers.

Accepts a finished PNG/PDF (rendered by whoever calls us) or raw printer
language (ZPL/EPL) and prints it via CUPS. It renders nothing itself, so it
can print ANY label.

Several printers run side by side; ``run.sh`` discovers them on boot and
registers one CUPS queue each. Callers pick one by name and default to the
DYMO queue, so existing integrations keep working unchanged.

Tested with a DYMO LabelWriter 400 (99014, 54 x 101 mm) and a Zebra ZD220D
(104 x 159 mm shipping labels), both attached through one USB hub.

Endpoints:
  GET  /            human-readable status page
  GET  /printers    which printers exist, their labels and accepted formats
  GET  /health      JSON status (printers connected? default media)
  GET  /journal     the last 100 print jobs and what was done to each
  POST /print       print PNG/PDF/JPG/ZPL — pick a printer with "printer"
  POST /selftest    print a small built-in test label

Which label is loaded per printer is add-on CONFIG (options: default_media,
zebra_label_size), on purpose: it mirrors the physical roll in the device, so
it must not be changeable from a client UI by accident. Clients only read it.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime

from flask import Flask, jsonify, request

app = Flask(__name__)

# The DYMO queue stays the default so every existing caller (the Fridge
# Assistant integration, Label Assistant, automations) keeps working when it
# doesn't name a printer.
DEFAULT_PRINTER = os.environ.get("PRINTER_NAME", "dymo")
DEFAULT_MEDIA = os.environ.get("DEFAULT_MEDIA", "w154h286")
MODEL = os.environ.get("PRINTER_MODEL", "")
PORT = int(os.environ.get("PORT", "8000"))

# What run.sh registered: [{name, kind, model, media, raster}, ...]. CUPS stays
# the source of truth for whether a queue is actually alive; this only carries
# the metadata CUPS doesn't know (which hardware family, configured media).
try:
    CONFIGURED = json.loads(os.environ.get("PRINTERS_JSON") or "[]")
except ValueError:
    CONFIGURED = []

RAW_FORMATS = ("zpl", "epl", "raw")
MAX_COPIES = 20

# Print-head resolution per hardware family. Rendering clients use this (via
# GET /printers) to produce pixel-perfect art instead of relying on scaling.
DPI_BY_KIND = {"dymo": 300, "zebra": 203}

# Bump when the JSON contract of /printers//health//print changes shape, so
# clients (which update on their own schedule) can feature-detect.
API_VERSION = 1


def _run(cmd, data: bytes | None = None):
    """Run a command; pass ``data`` to feed bytes to stdin."""
    return subprocess.run(cmd, capture_output=True, text=data is None,
                          input=data, timeout=60)


def _out(res) -> str:
    out = res.stdout
    return out.decode(errors="replace") if isinstance(out, bytes) else (out or "")


def _err(res) -> str:
    e = res.stderr
    return (e.decode(errors="replace") if isinstance(e, bytes) else (e or "")).strip()


# --------------------------------------------------------------------------
# Printers
# --------------------------------------------------------------------------
def _queues() -> list[str]:
    """Queue names CUPS currently has, in the order it lists them."""
    return re.findall(r"^printer (\S+)", _out(_run(["lpstat", "-p"])), re.M)


def _queue_exists(name: str) -> bool:
    return _run(["lpstat", "-p", name]).returncode == 0


def _default_queue() -> str:
    """The effective default: the configured queue, or else the first live one.

    Someone with only a Zebra attached must not see every call that omits a
    printer fail because the built-in default ("dymo") never got a queue.
    """
    queues = _queues()
    if DEFAULT_PRINTER in queues or not queues:
        return DEFAULT_PRINTER
    return queues[0]


def _media_of(name: str) -> str:
    m = re.search(r"PageSize=(\S+)", _out(_run(["lpoptions", "-p", name])))
    return m.group(1) if m else ""


def _has_driver(name: str) -> bool:
    """A queue with a PPD can rasterise images; a raw queue cannot."""
    return os.path.exists(f"/etc/cups/ppd/{name}.ppd")


def _supported_media(name: str) -> list[str]:
    """PageSize choices the queue's driver offers.

    Read from ``lpoptions -l`` (i.e. the PPD), so the list is whatever the
    driver actually accepts instead of a hard-coded catalogue per model.
    """
    for line in _out(_run(["lpoptions", "-p", name, "-l"])).splitlines():
        if line.startswith("PageSize"):
            choices = (c.lstrip("*") for c in line.split(":", 1)[1].split())
            # The literal Custom.WIDTHxHEIGHT token is a placeholder, not a size.
            return [c for c in choices if c != "Custom.WIDTHxHEIGHT"]
    return []


def _custom_media_ok(name: str) -> bool:
    """Whether the driver takes arbitrary Custom.WxH page sizes."""
    try:
        with open(f"/etc/cups/ppd/{name}.ppd", errors="replace") as f:
            return "*CustomPageSize" in f.read()
    except OSError:
        return False


def _media_points(media: str) -> tuple[int, int] | None:
    """The (width, height) in PostScript points a CUPS media name describes.

    ``w154h286`` and ``Custom.295x451`` both carry the physical size, so one
    parser covers every printer instead of a hard-coded table.
    """
    m = re.fullmatch(r"w(\d+)h(\d+)", media or "") or \
        re.fullmatch(r"Custom\.(\d+)x(\d+)(?:mm)?", media or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _media_label(media: str) -> str:
    """Human-readable size for a CUPS media name."""
    pts = _media_points(media)
    if pts:
        return f"{round(pts[0] * 25.4 / 72)} × {round(pts[1] * 25.4 / 72)} mm"
    return media or "onbekend"


def _configured(name: str) -> dict:
    for entry in CONFIGURED:
        if entry.get("name") == name:
            return entry
    return {}


_PPD_SIZES: dict[str, dict[str, tuple[float, float]]] = {}


def _ppd_sizes(queue: str) -> dict[str, tuple[float, float]]:
    """PaperDimension (in points) per media name, from the queue's PPD.

    The PPD is the geometry CUPS actually rasterizes with; media NAMES like
    w154h286 lie (DYMO's real page is 153.12 x 285.84 pt), so rendering from
    the name yields a slightly-off canvas that then gets rescaled.
    """
    if queue not in _PPD_SIZES:
        sizes = {}
        try:
            with open(f"/etc/cups/ppd/{queue}.ppd", errors="replace") as f:
                for ln in f:
                    m = re.match(r'\*PaperDimension\s+([^/:\s]+)[^:]*:\s*'
                                 r'"([\d.]+)\s+([\d.]+)"', ln)
                    if m:
                        sizes[m.group(1)] = (float(m.group(2)),
                                             float(m.group(3)))
        except OSError:
            pass
        _PPD_SIZES[queue] = sizes
    return _PPD_SIZES[queue]


# --------------------------------------------------------------------------
# Printable area (the part of the sticker the head can actually reach)
# --------------------------------------------------------------------------
# LabelWriters park the label with its leading edge past the print head (the
# gap sensor re-syncs a step counter against the CUTTER bar, not the head), so
# the first ~5 mm of every label is mechanically unreachable; the sides lose
# ~1-1.5 mm to sideways media play. DYMO encodes this per label as the PPD's
# ImageableArea, which is what we serve. Zebra desktop printers backfeed
# before every label (no leading dead zone; "Min. Print Length One Dot"), but
# Zebra's design guidance keeps ~1 mm off every edge against label wander.
# Sources: DYMO LW400 Tech Ref + DYMO CUPS PPDs, Zebra ZD220 spec sheet /
# ZD200 user guide, verified 2026-07.
DEFAULT_MARGINS_MM = {
    "dymo": {"leading": 5.4, "trailing": 1.5, "left": 1.5, "right": 1.0},
    "zebra": {"leading": 1.0, "trailing": 1.0, "left": 1.0, "right": 1.0},
}

_PPD_AREAS: dict[str, dict[str, tuple[float, float, float, float]]] = {}


def _ppd_areas(queue: str) -> dict[str, tuple[float, float, float, float]]:
    """ImageableArea (llx, lly, urx, ury in points) per media.

    Read from the pristine driver PPD in /usr/share/cups/model, NOT the
    installed queue PPD: _normalize_dymo_ppds() zeroes the queue's margins
    at boot (full bleed for 1:1 printing), which would make every label
    look margin-free here.
    """
    if queue not in _PPD_AREAS:
        areas = {}
        model = _configured(queue).get("model", "")
        candidates = [f"/usr/share/cups/model/{model}.ppd",
                      f"/etc/cups/ppd/{queue}.ppd"]
        for path in candidates:
            try:
                with open(path, errors="replace") as f:
                    for ln in f:
                        m = re.match(
                            r'\*ImageableArea\s+([^/:\s]+)[^:]*:\s*'
                            r'"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)"',
                            ln)
                        if m:
                            areas[m.group(1)] = tuple(
                                float(m.group(i)) for i in range(2, 6))
            except OSError:
                continue
            if areas:
                break
        _PPD_AREAS[queue] = areas
    return _PPD_AREAS[queue]


def _printable(media: str, dpi: int, queue: str,
               native: list[int] | None) -> dict | None:
    """Margins + pixel rect of the reachable part of this label.

    PPD ImageableArea when the media has one (PS origin is bottom-left and
    the page top leaves the printer first, so leading = height - ury);
    otherwise the per-kind hardware defaults. The leading margin doubles as
    the dead-zone compensation at print time (_drop_leading), so what this
    reports and what the printer does can never drift apart.
    """
    kind = _configured(queue).get("kind") or queue
    margins = dict(DEFAULT_MARGINS_MM.get(kind) or
                   {"leading": 0.0, "trailing": 0.0, "left": 0.0, "right": 0.0})
    area = _ppd_areas(queue).get(media)
    dims = _ppd_sizes(queue).get(media)
    if area and dims:
        llx, lly, urx, ury = area
        ppd = {"leading": (dims[1] - ury) * 25.4 / 72,
               "trailing": lly * 25.4 / 72,
               "left": llx * 25.4 / 72,
               "right": (dims[0] - urx) * 25.4 / 72}
        # All-zero = a full-bleed/generic PPD carrying no margin info (the
        # normalized queue PPD, sample.drv Zebra); keep the kind defaults.
        if any(v > 0.05 for v in ppd.values()):
            margins = ppd
    margins = {k: round(v, 1) for k, v in margins.items()}
    out: dict = {"margin_mm": margins}
    if native and len(native) == 2:
        x = round(margins["left"] * dpi / 25.4)
        y = round(margins["leading"] * dpi / 25.4)
        w = max(0, native[0] - x - round(margins["right"] * dpi / 25.4))
        h = max(0, native[1] - y - round(margins["trailing"] * dpi / 25.4))
        out["rect_px"] = {"x": x, "y": y, "w": w, "h": h}
    return out


def _native_px(media: str, dpi: int, queue: str = "") -> list[int] | None:
    """Exact pixel size of the raster CUPS will produce for this media.

    Named sizes use the PPD's PaperDimension (rounded to nearest, matching
    cupsRasterInterpretPPD); Custom.WxH sizes are truncated, matching
    imagetoraster's custom-size math (295 pt @203dpi -> 831 dots, not 832).
    """
    dims = _ppd_sizes(queue) if queue else {}
    if media in dims:
        return [round(p / 72 * dpi) for p in dims[media]]
    pts = _media_points(media)
    if not pts:
        return None
    if media.startswith("Custom."):
        return [int(p * dpi / 72) for p in pts]
    return [round(p / 72 * dpi) for p in pts]


_MEASURED_PX: dict[tuple[str, str], list[int]] = {}


def _probe_native_px(queue: str, media: str, dpi: int) -> list[int] | None:
    """The raster size CUPS *actually* produces, measured, not predicted.

    imagetoraster truncates float point-to-dot math, so a computed size can
    land one dot off (153.12 pt -> 637, not 638). Feeding a blank PNG through
    cupsfilter and reading the raster header back is the ground truth; the
    result is cached per (queue, media). Iterates once because for Custom.*
    sizes the raster dimensions depend on the input image's aspect ratio.
    """
    key = (queue, media)
    if key in _MEASURED_PX:
        return _MEASURED_PX[key]
    size = _native_px(media, dpi, queue)
    ppd = f"/etc/cups/ppd/{queue}.ppd"
    if not size or not os.path.exists(ppd):
        return size
    import io

    from PIL import Image
    for _ in range(2):
        img = Image.new("L", tuple(size), 255)
        buf = io.BytesIO()
        img.save(buf, format="PNG", dpi=(dpi, dpi))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(buf.getvalue())
            path = tmp.name
        try:
            res = subprocess.run(
                ["cupsfilter", "-p", ppd, "-m", "application/vnd.cups-raster",
                 "-o", f"PageSize={media}", "-o", "fit-to-page", path],
                capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return size
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        hdr = _parse_raster_header(res.stdout or b"")
        got = [hdr.get("cups_width"), hdr.get("cups_height")]
        if not all(got):
            return size
        if got == size:
            break
        size = got
    _MEASURED_PX[key] = size
    return size


def _media_entry(media: str, dpi: int, queue: str = "",
                 measured: bool = False) -> dict:
    """One media option: CUPS name, human size, and exact render size."""
    native = (_probe_native_px(queue, media, dpi) if measured
              else _native_px(media, dpi, queue))
    entry = {
        "media": media,
        "label": _media_label(media),
        # Render exactly this many pixels (portrait) for 1:1, unscaled output.
        "native_px": native,
    }
    # Which part of that canvas the head can actually reach; design clients
    # (Label Assistant, Fridge Assistant) keep their art inside this rect.
    if queue:
        printable = _printable(media, dpi, queue, native)
        if printable:
            entry["printable"] = printable
    return entry


def _printer_entry(name: str, default_name: str | None = None) -> dict:
    cfg = _configured(name)
    media = _media_of(name) or cfg.get("media", "")
    raster = _has_driver(name)
    accepts = ["png", "pdf", "jpg"] if raster else []
    # Zebra speaks ZPL natively; we hand raw jobs straight to the device.
    if cfg.get("kind") == "zebra":
        accepts.append("zpl")
    dpi = DPI_BY_KIND.get(cfg.get("kind"), 300)
    supported = _supported_media(name)
    if media and media not in supported:
        supported.insert(0, media)
    # The loaded label gets the measured (cached) size: it is the one that
    # actually prints, so its native_px must be exact, not just close.
    loaded = _media_entry(media, dpi, name, measured=True)
    if default_name is None:
        default_name = _default_queue()
    policy_mode, policy_align = _size_policy(name)
    return {
        "name": name,
        "kind": cfg.get("kind", "unknown"),
        "model": cfg.get("model") or (MODEL if name == DEFAULT_PRINTER else ""),
        "connected": _queue_exists(name),
        # media/label/native_px stay top-level for v0.2.0 clients; "loaded" is
        # the same thing for clients that also read "supported".
        "media": media,
        "label": loaded["label"],
        "dpi": dpi,
        "native_px": loaded["native_px"],
        "printable": loaded.get("printable"),
        "loaded": loaded,
        "supported": [_media_entry(m, dpi, name) for m in supported],
        "custom_media": _custom_media_ok(name),
        # How mismatched PNG/PDF sizes are handled, so clients can warn
        # before submitting instead of discovering a 422 after.
        "size_policy": {"mode": policy_mode, "align": policy_align},
        "accepts": accepts,
        "default": name == default_name,
    }


def _printer_list() -> list[dict]:
    default = _default_queue()
    return [_printer_entry(n, default) for n in _queues()]


def _status() -> dict:
    printers = _printer_list()
    return {
        "api_version": API_VERSION,
        # Kept for backwards compatibility with callers written against v0.1.
        "printer": _default_queue(),
        "model": MODEL,
        "connected": any(p["connected"] for p in printers),
        "default_media": DEFAULT_MEDIA,
        "printers": printers,
        "queue": _out(_run(["lpstat", "-o"])).strip(),
        "devices": _out(_run(["lpstat", "-v"])).strip(),
    }


def _default_media_for(printer: str) -> str:
    # The loaded label is add-on CONFIG, deliberately: it mirrors the physical
    # roll, so it should not be flippable from a client UI by accident.
    live = _media_of(printer)
    if live:
        return live
    cfg = _configured(printer)
    return cfg.get("media") or (DEFAULT_MEDIA if printer == DEFAULT_PRINTER else "")


# --------------------------------------------------------------------------
# Dead-zone compensation (feed direction, DYMO only)
# --------------------------------------------------------------------------
def _addon_options() -> dict:
    """The add-on's configuration, as written by the Supervisor."""
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _dead_zone_mm(printer: str, media: str) -> float:
    """How much of the raster's leading edge the head can never deliver.

    LabelWriters park the label with its leading edge past the print head
    (the gap sensor re-syncs against the cutter bar), so raster row 0 lands
    ~5 mm INTO the label: unshifted, everything prints that much late and
    the far end falls off. Dropping exactly the label's leading margin (the
    same printable-area data served to clients, who keep art out of that
    strip anyway) restores 1:1 registration with no knobs to tune. Zebra
    printers backfeed before every label, so their advisory margins need no
    compensation.
    """
    if _configured(printer).get("kind") != "dymo":
        return 0.0
    printable = _printable(media, DPI_BY_KIND.get("dymo", 300), printer, None)
    if not printable:
        return 0.0
    return float(printable["margin_mm"].get("leading") or 0.0)


def _drop_leading(data: bytes, mm: float, dpi: int) -> bytes:
    """Crop ``mm`` off the raster's top and pad white at the bottom: the
    cropped strip is exactly the label that already passed the head."""
    import io

    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        dy = round(mm * dpi / 25.4)
        if not dy:
            return data
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        out = Image.new(img.mode, img.size,
                        255 if img.mode == "L" else (255, 255, 255))
        out.paste(img, (0, -dy))
        buf = io.BytesIO()
        out.save(buf, format="PNG", dpi=(dpi, dpi))
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - never break printing over this
        return data


# --------------------------------------------------------------------------
# Size policy: what to do when a PNG/PDF/JPG does not match the loaded label
# --------------------------------------------------------------------------
# "scale" is CUPS fit-to-page: aspect kept, never cropped, but barcodes end
# up off-pitch. For size-critical art the config can forbid scaling per
# printer: "crop" lays the image on the label 1:1 (one image pixel = one
# printer dot, alignment configurable, overhang falls off) and "reject"
# refuses the job so the caller notices. PDFs are rasterized at head dpi
# first (ghostscript — the same rasterizer CUPS uses), which preserves their
# physical mm size; pt->px rounding can land a page a dot or two off, which
# is tolerated and then padded/cropped to exact. Raw ZPL never comes here.
PDF_PX_TOLERANCE = 2


def _size_policy(printer: str) -> tuple[str, str]:
    """(mode, align) for a queue, from the add-on config."""
    kind = _configured(printer).get("kind") or printer
    opts = _addon_options()
    mode = str(opts.get(f"{kind}_size_mismatch") or "scale").strip().lower()
    align = str(opts.get(f"{kind}_crop_align") or "center").strip().lower()
    return (mode if mode in ("scale", "crop", "reject") else "scale"), align


def _policy_target_px(printer: str, media: str) -> list[int] | None:
    """The exact raster the job must produce, or None when unknowable
    ("auto": the LW550 picks its own roll, so there is nothing to compare
    against and the job falls back to the scale path)."""
    if not media or media.lower() == "auto":
        return None
    dpi = DPI_BY_KIND.get(_configured(printer).get("kind"), 300)
    return _probe_native_px(printer, media, dpi) or _native_px(media, dpi, printer)


def _rasterize_pdf(data: bytes, dpi: int) -> list | None:
    """PDF -> one grayscale PIL image per page at head resolution."""
    from PIL import Image
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.pdf")
        with open(src, "wb") as f:
            f.write(data)
        res = _run(["gs", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                    "-sDEVICE=pnggray", f"-r{dpi}",
                    "-o", os.path.join(td, "page-%03d.png"), src])
        if res.returncode != 0:
            return None
        pages = []
        for name in sorted(os.listdir(td)):
            if name.startswith("page-"):
                img = Image.open(os.path.join(td, name))
                img.load()
                pages.append(img)
        return pages or None


def _apply_size_policy(data: bytes, fmt: str, media: str, printer: str,
                       mode: str, align: str, notes: list | None = None):
    """Enforce crop/reject on an image job.

    Returns a list of PNG pages sized exactly to the label, None when the
    target is unknowable (caller keeps the scale path), or an error dict."""
    import io

    from PIL import Image

    def note(msg: str) -> None:
        if notes is not None:
            notes.append(msg)

    target = _policy_target_px(printer, media)
    if not target:
        return None
    tw, th = target
    dpi = DPI_BY_KIND.get(_configured(printer).get("kind"), 300)
    if fmt == "pdf":
        pages = _rasterize_pdf(data, dpi)
        if pages is None:
            note("Ghostscript kon de PDF niet rasteren")
            return {"ok": False, "error": "pdf_raster_failed",
                    "printer": printer,
                    "hint": "Ghostscript could not read this PDF."}
        note(f"PDF gerasterd op {dpi}dpi: {len(pages)} pagina('s)")
        tol = PDF_PX_TOLERANCE
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:  # noqa: BLE001
            note("afbeelding niet te decoderen")
            return {"ok": False, "error": "bad_image", "printer": printer,
                    "hint": "Could not decode the image."}
        pages, tol = [img], 0
    out = []
    for pageno, img in enumerate(pages):
        # A landscape design that is exactly the label turned sideways is
        # unambiguous: turn it instead of cropping it to ribbons.
        if img.size != (tw, th) and img.size[::-1] == (tw, th):
            img = img.rotate(90, expand=True)
            if not pageno:
                note("landschap-ontwerp 90° gedraaid (past exact gedraaid)")
        w, h = img.size
        if mode == "reject" and (abs(w - tw) > tol or abs(h - th) > tol):
            note(f"geweigerd: {w}×{h}px past niet op label {tw}×{th}px "
                 "(size policy 'reject')")
            return {"ok": False, "error": "size_mismatch", "printer": printer,
                    "media": media, "expected_px": [tw, th],
                    "got_px": [w, h], "dpi": dpi,
                    "hint": "Size policy is 'reject': render exactly "
                            "expected_px (GET /printers -> native_px), or "
                            "set the size_mismatch option to scale/crop."}
        if (w, h) != (tw, th):
            acts = []
            if w > tw or h > th:
                acts.append("overhang eraf")
            if w < tw or h < th:
                acts.append("wit aangevuld")
            if not pageno:
                note(f"{w}×{h}px 1:1 op label {tw}×{th}px gelegd "
                     f"({align}: {', '.join(acts)}) — niet geschaald")
            if img.mode == "RGBA":
                flat = Image.new("RGB", img.size, (255, 255, 255))
                flat.paste(img, mask=img.split()[3])
                img = flat
            elif img.mode not in ("L", "RGB"):
                img = img.convert("RGB")
            canvas = Image.new(img.mode, (tw, th),
                               255 if img.mode == "L" else (255, 255, 255))
            pos = (0, 0) if align.startswith("leading") \
                else ((tw - w) // 2, (th - h) // 2)
            canvas.paste(img, pos)
            img = canvas
        elif not pageno:
            note(f"maat exact {tw}×{th}px → 1:1 doorgezet")
        buf = io.BytesIO()
        img.save(buf, format="PNG", dpi=(dpi, dpi))
        out.append(buf.getvalue())
    return out


# --------------------------------------------------------------------------
# Print journal: what happened to every job (scaled? cropped? 1:1?)
# --------------------------------------------------------------------------
# Every job that reaches _print_bytes gets one entry describing exactly what
# was done to it before it hit the printer, so "why does my label look like
# this" never needs guesswork. Ring of the last 100, persisted across
# restarts in /data, mirrored to stdout (the add-on Log tab).
JOURNAL_MAX = 100
JOURNAL_PATH = "/data/print_journal.jsonl"
_JOURNAL: list[dict] = []


def _journal_load() -> None:
    try:
        with open(JOURNAL_PATH) as f:
            entries = [json.loads(ln) for ln in f if ln.strip()]
        _JOURNAL.extend(entries[-JOURNAL_MAX:])
    except (OSError, ValueError):
        pass


def _journal_add(entry: dict) -> None:
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **entry}
    _JOURNAL.append(entry)
    del _JOURNAL[:-JOURNAL_MAX]
    icon = "ok" if entry.get("ok") else "FOUT"
    print(f"[print] {entry['time']} {entry.get('printer')} "
          f"({entry.get('format')}, {icon}): {entry.get('summary')}",
          flush=True)
    try:
        with open(JOURNAL_PATH, "w") as f:
            for e in _JOURNAL:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------
def _sniff_format(data: bytes) -> str:
    """Detect what we were handed: pdf / png / jpg / zpl."""
    head = data[:200].lstrip()
    if head[:5] == b"%PDF-":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    # ZPL always opens a label with ^XA (or a ~ control command).
    if head[:3] == b"^XA" or head[:2] == b"~J" or head[:2] == b"^X":
        return "zpl"
    return "png"


def _print_bytes(data: bytes, media: str | None, copies: int, printer: str,
                 fmt: str | None = None, source: str = "api") -> dict:
    """Print + journal: every job records what was done to it on the way."""
    notes: list[str] = []
    result = _do_print(data, media, copies, printer, fmt, notes)
    entry = {
        "printer": printer,
        "source": source,
        "ok": bool(result.get("ok")),
        "format": result.get("format") or (fmt or "?"),
        "media": result.get("media") or media or "auto",
        "copies": result.get("copies", copies),
        "bytes": len(data),
        "summary": "; ".join(notes) or "geen bewerkingen geregistreerd",
    }
    if not entry["ok"]:
        entry["error"] = result.get("error")
        detail = result.get("detail") or result.get("hint")
        if detail:
            entry["detail"] = str(detail)[:200]
    _journal_add(entry)
    return result


def _src_px(data: bytes, fmt: str) -> list[int] | None:
    """Pixel size of an incoming PNG/JPG, for the journal."""
    if fmt not in ("png", "jpg"):
        return None
    import io

    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as img:
            return list(img.size)
    except Exception:  # noqa: BLE001 - journal detail, never blocks printing
        return None


def _do_print(data: bytes, media: str | None, copies: int, printer: str,
              fmt: str | None, notes: list[str]) -> dict:
    if not _queue_exists(printer):
        known = _queues()
        notes.append(f"queue '{printer}' bestaat niet of is offline")
        return {"ok": False, "error": "printer_not_connected", "printer": printer,
                "available": known,
                "hint": f"Unknown or offline queue '{printer}'. "
                        f"Available: {', '.join(known) or 'none'}. "
                        "Connect + power on the printer, then restart the add-on."}
    fmt = (fmt or _sniff_format(data)).lower()
    copies = max(1, min(int(copies or 1), MAX_COPIES))

    if fmt in RAW_FORMATS:
        # Raw printer language goes to the device untouched — the job already
        # *is* what the printer speaks, so CUPS must not filter or re-render it
        # (-l). Page size lives inside the ZPL itself.
        blocks = data.count(b"^XA")
        dg = data.count(b"~DG")
        info = f"{blocks} format(s)"
        if dg:
            info += f", {dg} graphic-download(s)"
        pw = re.search(rb"\^PW(\d+)", data)
        ll = re.search(rb"\^LL(\d+)", data)
        if pw:
            info += f", ^PW{pw.group(1).decode()}"
        if ll:
            info += f", ^LL{ll.group(1).decode()}"
        notes.append(f"raw {fmt.upper()} 1:1 doorgezet, niets aangepast ({info})")
        res = _run(["lpr", "-P", printer, "-#", str(copies), "-l"], data=data)
        if res.returncode != 0:
            notes.append("lpr weigerde de job")
            return {"ok": False, "error": "lpr_failed", "detail": _err(res),
                    "printer": printer, "format": fmt}
        return {"ok": True, "printed": True, "printer": printer, "format": fmt,
                "copies": copies, "media": "raw"}

    media = (media or "").strip()
    pages = [data]
    mode, align = _size_policy(printer)
    policy = "scale"
    if mode in ("crop", "reject") and fmt in ("png", "jpg", "pdf"):
        enforced = _apply_size_policy(data, fmt, media, printer, mode, align,
                                      notes)
        if isinstance(enforced, dict):
            return enforced
        if enforced is not None:
            pages, fmt, policy = enforced, "png", mode

    if policy == "scale":
        # fit-to-page does the fitting in CUPS; work out here what that will
        # amount to, so the journal can say "1:1" or "x% geschaald". Empty or
        # "auto" media means CUPS falls back to the queue's default PageSize —
        # the loaded label — so resolve that for an exact journal note.
        eff = media
        via_loaded = False
        if not eff or eff.lower() == "auto":
            eff = _default_media_for(printer)
            via_loaded = bool(eff) and eff.lower() != "auto"
        target = _policy_target_px(printer, eff) if eff else None
        src = _src_px(data, fmt)
        if not target:
            notes.append("printer kiest zelf de rol (media 'auto') → "
                         "driver past in (fit-to-page)")
        else:
            tw, th = target
            desc = (f"geladen label {_media_label(eff)}" if via_loaded
                    else f"label {_media_label(eff)}")
            if fmt == "pdf":
                notes.append(f"PDF → past in {desc} = {tw}×{th}px "
                             "(fit-to-page)")
            elif src:
                if src == [tw, th]:
                    notes.append(f"maat exact {tw}×{th}px → 1:1 doorgezet "
                                 f"({desc})")
                else:
                    s = min(tw / src[0], th / src[1])
                    pct = (s - 1) * 100
                    woord = "vergroot" if pct > 0 else "verkleind"
                    msg = (f"{src[0]}×{src[1]}px ≠ {desc} {tw}×{th}px → "
                           f"{abs(pct):.1f}% {woord} (fit-to-page)")
                    if abs(tw / th - src[0] / src[1]) > 0.01:
                        msg += ", verhouding wijkt af → witruimte aan één kant"
                    notes.append(msg)

    dead_mm = _dead_zone_mm(printer, media or _default_media_for(printer))
    if dead_mm and fmt in ("png", "jpg"):
        dpi = DPI_BY_KIND.get(_configured(printer).get("kind"), 300)
        pages = [_drop_leading(p, dead_mm, dpi) for p in pages]
        fmt = "png"
        notes.append(f"DYMO dode zone: eerste {dead_mm:g}mm afgeknipt, "
                     "onderaan wit aangevuld")

    paths = []
    try:
        for p in pages:
            with tempfile.NamedTemporaryFile(suffix=f".{fmt}",
                                             delete=False) as tmp:
                tmp.write(p)
                paths.append(tmp.name)
        cmd = ["lpr", "-P", printer, "-#", str(copies)]
        # The LabelWriter 550 auto-detects the loaded roll (ALR). Forcing a
        # mismatched PageSize makes it hold the job, so "auto" (or empty) lets
        # the driver decide and we only pass PageSize when asked.
        if media and media.lower() != "auto":
            cmd += ["-o", f"PageSize={media}"]
        # Under crop/reject the pages already ARE the label raster, so
        # fit-to-page is a no-op there; under scale it does the fitting.
        cmd += ["-o", "fit-to-page"] + paths
        res = _run(cmd)
        if res.returncode != 0:
            return {"ok": False, "error": "lpr_failed", "printer": printer,
                    "detail": _err(res) or _out(res).strip()}
        return {"ok": True, "printed": True, "printer": printer,
                "media": media or "auto", "copies": copies, "format": fmt,
                "size_policy": policy, "pages": len(paths)}
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _extract_request():
    """Return (bytes, media, copies, printer, format) from the request.

    Accepts three shapes: multipart upload, JSON (base64 image or ZPL text),
    or a raw body with query parameters. ``printer`` stays None when the
    caller named none, so the effective default can be resolved at print time
    (it may fall back to another queue when the configured one is absent).
    """
    if request.files.get("file"):
        f = request.files["file"]
        return (f.read(), request.form.get("media"),
                request.form.get("copies", 1),
                request.form.get("printer") or None,
                request.form.get("format"))
    if request.is_json:
        body = request.get_json(silent=True) or {}
        printer = body.get("printer") or None
        media, copies, fmt = body.get("media"), body.get("copies", 1), body.get("format")
        zpl = body.get("zpl") or body.get("raw")
        if zpl:  # plain-text ZPL is the friendliest payload for a webshop
            return zpl.encode(), media, copies, printer, fmt or "zpl"
        b64 = body.get("image_base64") or body.get("png_base64") or body.get("data")
        if not b64:
            return None, media, copies, printer, fmt
        return base64.b64decode(b64), media, copies, printer, fmt
    if request.data:
        return (request.data, request.args.get("media"),
                request.args.get("copies", 1),
                request.args.get("printer") or None,
                request.args.get("format"))
    return None, None, 1, None, None


@app.route("/print", methods=["POST"])
def print_label():
    data, media, copies, printer, fmt = _extract_request()
    if not data:
        return jsonify({"ok": False, "error": "no_image",
                        "hint": "POST a PNG/PDF/ZPL as multipart 'file', or JSON "
                                "{image_base64|zpl, printer, media, copies}."}), 400
    printer = printer or _default_queue()
    if media is None:
        media = _default_media_for(printer)
    result = _print_bytes(data, media, copies, printer, fmt)
    if result.get("ok"):
        return jsonify(result), 200
    # A size-policy rejection is the caller's problem (fix the render size),
    # not a printer outage — signal it as such.
    client_errors = ("size_mismatch", "bad_image", "pdf_raster_failed")
    return jsonify(result), (422 if result.get("error") in client_errors
                             else 503)


@app.route("/printers", methods=["GET"])
def printers():
    """What can I print on, with which labels, in which formats?"""
    return jsonify({"api_version": API_VERSION, "default": _default_queue(),
                    "printers": _printer_list()})


@app.route("/selftest", methods=["POST"])
def selftest():
    printer = (request.args.get("printer")
               or (request.get_json(silent=True) or {}).get("printer")
               or _default_queue())
    if _configured(printer).get("kind") == "zebra":
        # Native ZPL, so the test also proves the raw passthrough works.
        zpl = (
            "^XA^CI28"
            "^FO40,60^A0N,60,60^FDLABEL PRINTER OK^FS"
            f"^FO40,140^A0N,40,40^FD{printer} - raw ZPL^FS"
            "^FO40,200^GB700,4,4^FS"
            "^FO40,240^BY3^BCN,120,Y,N,N^FDSELFTEST^FS"
            "^XZ"
        ).encode()
        result = _print_bytes(zpl, None, 1, printer, "zpl", source="selftest")
        return jsonify(result), (200 if result.get("ok") else 503)

    import io

    from PIL import Image, ImageDraw
    img = Image.new("L", (642, 1192), 255)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([12, 12, 629, 1179], radius=30, outline=0, width=3)
    d.text((60, 80), "LABEL PRINTER OK", fill=0)
    d.text((60, 140), f"{printer} - {_media_label(_media_of(printer))}", fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(300, 300))
    result = _print_bytes(buf.getvalue(), _default_media_for(printer), 1,
                          printer, source="selftest")
    return jsonify(result), (200 if result.get("ok") else 503)


@app.route("/health", methods=["GET"])
def health():
    return jsonify(_status())


@app.route("/journal", methods=["GET"])
def journal():
    """The last print jobs (newest first) and what was done to each."""
    try:
        limit = int(request.args.get("limit", JOURNAL_MAX))
    except ValueError:
        limit = JOURNAL_MAX
    jobs = list(reversed(_JOURNAL))[:max(1, min(limit, JOURNAL_MAX))]
    return jsonify({"count": len(_JOURNAL), "max": JOURNAL_MAX, "jobs": jobs})


@app.route("/debug", methods=["GET"])
def debug():
    def tail(path, n=12000):
        try:
            with open(path, errors="replace") as f:
                return f.read()[-n:]
        except OSError as e:
            return f"(cannot read {path}: {e})"
    status = _out(_run(["sh", "-c",
        "grep -aE 'Job 1|CheckLock|CheckLabel|CheckStatus|ReadStatus:|STATE:|"
        "counterfeit|Sending|pages|Wrote .* print|CheckPrintHead|Reprint' "
        "/var/log/cups/error_log | tail -40"]))
    # The PPDs' geometry lines are the ground truth for how CUPS positions a
    # job on the label (margins shift/clip full-bleed art), so expose them.
    geometry = {}
    for q in _queues():
        geometry[q] = _out(_run(["sh", "-c",
            f"grep -aE 'ImageableArea|PaperDimension|HWMargins|MaxMediaWidth|"
            f"MaxMediaHeight|CustomPageSize|ParamCustom|LandscapeOrientation|"
            f"DefaultPageSize' /etc/cups/ppd/{q}.ppd"]))
    return jsonify({
        "configured": CONFIGURED,
        "printers": _out(_run(["lpstat", "-l", "-p"])),
        "jobs": _out(_run(["lpstat", "-l", "-o"])),
        "devices": _out(_run(["lpinfo", "-v"])),
        "geometry": geometry,
        "status_lines": status,
        "error_log": tail("/var/log/cups/error_log"),
    })


# --------------------------------------------------------------------------
# Pipeline inspection (no paper involved)
# --------------------------------------------------------------------------
def _parse_raster_header(data: bytes) -> dict:
    """Geometry fields from a CUPS raster v2/v3 page header."""
    import struct
    sync = data[:4]
    endian = {b"RaS2": ">", b"RaS3": ">", b"2SaR": "<", b"3SaR": "<"}.get(sync)
    if not endian:
        return {"error": f"not a cups raster stream (sync={sync!r})"}
    h = data[4:4 + 1796]
    u = lambda off: struct.unpack(endian + "I", h[off:off + 4])[0]
    return {
        "sync": sync.decode(),
        "hw_resolution": [u(276), u(280)],
        "imaging_bbox_pts": [u(284), u(288), u(292), u(296)],
        "page_size_pts": [u(352), u(356)],
        "cups_width": u(372),
        "cups_height": u(376),
        "bits_per_pixel": u(388),
        "bytes_per_line": u(392),
    }


def _decode_zpl_dg(body: str, per_row: int, total: int) -> dict:
    """Decode ZPL ASCII graphic data (plain hex + RLE) to the black bbox.

    RLE per the ZPL manual: G..Z repeat the next nibble 1..20 times,
    g..z repeat it 20,40,..400 times, ',' zero-fills the rest of the row,
    '!' one-fills it, ':' repeats the previous row.
    """
    rows, row, prev, repeat = [], [], [], 0
    for ch in body:
        if len(rows) * per_row * 2 >= total * 2:
            break
        if ch in "0123456789ABCDEFabcdef":
            row.extend([int(ch, 16)] * (repeat or 1)); repeat = 0
        elif "G" <= ch <= "Z":
            repeat += ord(ch) - ord("G") + 1
        elif "g" <= ch <= "z":
            repeat += (ord(ch) - ord("g") + 1) * 20
        elif ch == ",":
            row.extend([0] * (per_row * 2 - len(row)))
        elif ch == "!":
            row.extend([0xF] * (per_row * 2 - len(row)))
        elif ch == ":":
            rows.append(list(prev)); continue
        if len(row) >= per_row * 2:
            prev = row[:per_row * 2]
            rows.append(prev)
            row = row[per_row * 2:]
    xmin = xmax = ymin = ymax = None
    for y, r in enumerate(rows):
        for xn, nib in enumerate(r):
            if not nib:
                continue
            for b in range(4):
                if nib & (0x8 >> b):
                    x = xn * 4 + b
                    xmin = x if xmin is None or x < xmin else xmin
                    xmax = x if xmax is None or x > xmax else xmax
                    ymin = y if ymin is None else ymin
                    ymax = y
    return {"rows_decoded": len(rows), "bitmap_width_dots": per_row * 8,
            "black_bbox": None if xmin is None else
            {"x": [xmin, xmax], "y": [ymin, ymax]}}


@app.route("/debug/pipeline", methods=["GET"])
def debug_pipeline():
    """Run the real CUPS filter chain on a test image and report geometry.

    A 1px border + top-left corner square at exactly native_px goes through
    cupsfilter with the queue's PPD; the intermediate raster header and (for
    ZPL queues) the decoded device bitmap tell us 1:1 whether anything gets
    scaled, shifted or clipped before it ever reaches the printer.
    """
    import io

    from PIL import Image, ImageDraw
    printer = request.args.get("printer") or _default_queue()
    ppd = f"/etc/cups/ppd/{printer}.ppd"
    if not os.path.exists(ppd):
        return jsonify({"error": f"no PPD for queue '{printer}'"}), 404
    media = request.args.get("media") or _default_media_for(printer)
    dpi = DPI_BY_KIND.get(_configured(printer).get("kind"), 300)
    native = _probe_native_px(printer, media, dpi)
    if not native:
        return jsonify({"error": f"cannot parse media '{media}'"}), 400

    img = Image.new("L", tuple(native), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, native[0] - 1, native[1] - 1], outline=0, width=1)
    d.rectangle([0, 0, 40, 40], fill=0)  # top-left marker: orientation check
    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(dpi, dpi))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(buf.getvalue())
        png_path = tmp.name

    opts = ["-o", f"PageSize={media}", "-o", "fit-to-page"]
    report = {"printer": printer, "media": media, "dpi": dpi,
              "png_px": native}
    try:
        return _debug_pipeline_run(report, ppd, printer, media, opts, png_path)
    except Exception as err:  # noqa: BLE001 - diagnostics must report, not 500
        import traceback
        report["error"] = f"{type(err).__name__}: {err}"
        report["trace"] = traceback.format_exc()[-1500:]
        return jsonify(report), 500
    finally:
        try:
            os.unlink(png_path)
        except OSError:
            pass


def _debug_pipeline_run(report, ppd, printer, media, opts, png_path):
    try:
        chain = _run(["cupsfilter", "--list-filters", "-p", ppd,
                      "-m", f"printer/{printer}", *opts, png_path])
        report["filter_chain"] = (_out(chain) + _err(chain)).strip().splitlines()

        rast = subprocess.run(
            ["cupsfilter", "-p", ppd, "-m", "application/vnd.cups-raster",
             *opts, png_path], capture_output=True, timeout=60)
        report["raster"] = _parse_raster_header(rast.stdout) if rast.stdout \
            else {"error": (rast.stderr or b"")[-400:].decode(errors="replace")}

        # cupsfilter's cost-based chain can dodge the driver filter, so run it
        # exactly as cupsd does: raster in on stdin, PPD via the environment.
        driver = None
        for line in open(ppd, errors="replace"):
            if line.startswith("*cupsFilter") and "cups-raster" in line:
                driver = line.split()[-1].strip('"')
        report["driver_filter"] = driver
        driver_path = f"/usr/lib/cups/filter/{driver}" if driver else ""
        if driver and not os.path.exists(driver_path):
            hits = _out(_run(["sh", "-c",
                              f"find / -name '{driver}' -type f 2>/dev/null"]))
            driver_path = hits.strip().splitlines()[0] if hits.strip() else ""
            report["driver_path"] = driver_path or "NOT FOUND"
        # Only ZPL output is analyzable text; the DYMO filter also blocks on
        # its language monitor when run outside cupsd, so skip it there.
        if driver_path and rast.stdout and driver == "rastertolabel":
            dev = subprocess.run(
                [driver_path, "1", "debug", "pipeline", "1",
                 f"PageSize={media}"],
                input=rast.stdout, capture_output=True, timeout=60,
                env={**os.environ, "PPD": ppd})
        else:
            dev = subprocess.run(
                ["cupsfilter", "-p", ppd, "-m", f"printer/{printer}", *opts,
                 png_path], capture_output=True, timeout=60)
        raw = dev.stdout or b""
        report["device_bytes"] = len(raw)
        text = raw.decode("ascii", errors="replace")
        m = re.search(r"~DG(?:R:)?([A-Z.]+\.GRF),(\d+),(\d+),?", text)
        if m:  # ZPL job: measure exactly what the printer will draw
            total, per_row = int(m.group(2)), int(m.group(3))
            body = text[m.end():]
            end = body.find("^XA")
            report["zpl"] = {
                "graphic_total_bytes": total,
                "bytes_per_row": per_row,
                "commands": re.findall(
                    r"\^(?:PW|LL|LS|LH|LT|PO|MN|PQ|PR|MD|XG)[^\^~\n]*",
                    text[:m.start()] + (body[end:] if end >= 0 else "")),
                **_decode_zpl_dg(body[:end] if end >= 0 else body,
                                 per_row, total),
            }
        else:
            report["device_head"] = repr(raw[:200])
    finally:
        try:
            os.unlink(png_path)
        except OSError:
            pass
    return jsonify(report)


@app.route("/", methods=["GET"])
def index():
    import html as _html

    st = _status()
    rows = "".join(
        f"<tr><td><code>{p['name']}</code>{' <b>· default</b>' if p['default'] else ''}</td>"
        f"<td>{p['model'] or p['kind']}</td><td>{p['label']}</td>"
        f"<td>{', '.join(p['accepts']) or '—'}</td>"
        f"<td>{'🟢' if p['connected'] else '🔴'}</td></tr>"
        for p in st["printers"]
    ) or "<tr><td colspan='5'>Geen printers gevonden.</td></tr>"
    jobs = "".join(
        f"<tr><td class='t'>{_html.escape(j.get('time', ''))}</td>"
        f"<td><code>{_html.escape(str(j.get('printer', '?')))}</code></td>"
        f"<td>{_html.escape(str(j.get('format', '?')))}"
        f"{' ×' + str(j['copies']) if j.get('copies', 1) != 1 else ''}</td>"
        f"<td>{'🟢' if j.get('ok') else '🔴'}</td>"
        f"<td>{_html.escape(str(j.get('summary', '')))}"
        f"{('<br><small>' + _html.escape(str(j.get('detail', ''))) + '</small>') if j.get('detail') else ''}"
        f"</td></tr>"
        for j in reversed(_JOURNAL[-20:])
    ) or "<tr><td colspan='5'>Nog geen printjobs sinds de start.</td></tr>"
    return (
        "<html><head><title>Label Printer</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{font-family:system-ui,sans-serif;max-width:700px;margin:40px "
        "auto;padding:0 20px;line-height:1.5}code{background:#f4f4f5;padding:2px 6px;"
        "border-radius:6px}pre{background:#f4f4f5;padding:14px;border-radius:10px;"
        "overflow:auto;font-size:13px}table{border-collapse:collapse;width:100%}"
        "td,th{padding:8px 10px;border-bottom:1px solid #e5e5e5;text-align:left;"
        "font-size:14px;vertical-align:top}td.t{white-space:nowrap;"
        "font-family:ui-monospace,monospace;font-size:12px;color:#666}"
        "small{color:#888}</style></head><body>"
        "<h1>🖨️ Label Printer</h1>"
        "<table><tr><th>Queue</th><th>Model</th><th>Labels</th>"
        f"<th>Accepteert</th><th></th></tr>{rows}</table>"
        "<h2>Laatste printjobs</h2>"
        "<p>Per job staat hier wat ermee gebeurd is: 1:1 doorgezet, geschaald, "
        "bijgesneden of geweigerd. Volledige historie (laatste "
        f"{JOURNAL_MAX}): <code>GET /journal</code>.</p>"
        "<table><tr><th>Tijd</th><th>Printer</th><th>Formaat</th><th></th>"
        f"<th>Wat is er gebeurd</th></tr>{jobs}</table>"
        "<h3>PNG/PDF printen</h3>"
        "<pre>curl -F file=@label.png -F printer=dymo http://HOST:8000/print</pre>"
        "<h3>Raw ZPL printen</h3>"
        "<pre>curl -H 'Content-Type: application/json' \\\n"
        "  -d '{\"printer\":\"zebra\",\"zpl\":\"^XA^FO50,50^A0N,50,50^FDHi^FS^XZ\"}' \\\n"
        "  http://HOST:8000/print</pre>"
        "<h3>Printers opvragen / testen</h3>"
        "<pre>curl http://HOST:8000/printers\n"
        "curl -X POST 'http://HOST:8000/selftest?printer=zebra'</pre>"
        f"<h3>Status</h3><pre>{json.dumps(st, indent=2)}</pre>"
        "</body></html>"
    )


# --------------------------------------------------------------------------
# Boot-time geometry normalization
# --------------------------------------------------------------------------
def _normalize_dymo_ppds() -> None:
    """Zero the per-media ImageableArea margins in DYMO PPDs (full bleed).

    raster2dymolw prints the raster starting at head dot 0 and ignores all
    margin/position metadata, while fit-to-page SCALES the image into the
    ImageableArea — so with DYMO's stock margins every label printed ~5-8%
    too small and never 1:1. Zeroing the margins makes the imageable area
    equal the page: scale factor exactly 1.0, pixel column i = head dot i.
    Media wider than the printhead keep their margins: full bleed there
    would right-truncate at the head instead.
    """
    head_dots = {"lw4xl": 1248, "lw5xl": 1248}  # everything else: 672
    for entry in CONFIGURED:
        if entry.get("kind") != "dymo":
            continue
        path = f"/etc/cups/ppd/{entry['name']}.ppd"
        head = head_dots.get(entry.get("model", ""), 672)
        try:
            with open(path, errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        dims = {}
        for ln in lines:
            m = re.match(r'\*PaperDimension\s+([^/:\s]+)[^:]*:\s*'
                         r'"([\d.]+)\s+([\d.]+)"', ln)
            if m:
                dims[m.group(1)] = (float(m.group(2)), float(m.group(3)))
        out, changed = [], 0
        for ln in lines:
            m = re.match(r'(\*ImageableArea\s+([^/:\s]+)[^:]*):\s*"[^"]*"', ln)
            if m and m.group(2) in dims:
                w, h = dims[m.group(2)]
                if w * 300 / 72 <= head:
                    new = f'{m.group(1)}: "0 0 {w:g} {h:g}"\n'
                    if new != ln:
                        changed += 1
                    out.append(new)
                    continue
            out.append(ln)
        if changed:
            with open(path, "w") as f:
                f.writelines(out)
            print(f"[geometry] {path}: {changed} media made full-bleed 1:1",
                  flush=True)


def _reset_zebra_geometry() -> None:
    """Persist Label Shift 0 / Label Home 0,0 on every Zebra at boot.

    rastertolabel never sends ^LS, so a Left Position stored in the printer
    (by e.g. Zebra Setup Utilities or a Windows driver) silently shifts every
    CUPS job sideways and clips one edge. This pins the geometry defaults;
    the config-only format prints nothing and feeds no label.
    """
    for entry in CONFIGURED:
        if entry.get("kind") == "zebra" and _queue_exists(entry["name"]):
            res = _run(["lpr", "-P", entry["name"], "-l"],
                       data=b"^XA^LS0^LH0,0^JUS^XZ\n")
            print(f"[geometry] {entry['name']}: ^LS0^LH0,0 saved "
                  f"({'ok' if res.returncode == 0 else _err(res)})",
                  flush=True)


def _warm_native_px() -> None:
    """Measure the loaded media of every queue up front, so the first
    /printers call answers instantly with exact geometry."""
    for entry in CONFIGURED:
        name = entry.get("name", "")
        media = _default_media_for(name)
        if not name or not media or media == "auto":
            continue
        dpi = DPI_BY_KIND.get(entry.get("kind"), 300)
        px = _probe_native_px(name, media, dpi)
        print(f"[geometry] {name}: {media} -> native_px {px}", flush=True)


if __name__ == "__main__":
    _journal_load()
    _normalize_dymo_ppds()
    _reset_zebra_geometry()
    _warm_native_px()
    app.run(host="0.0.0.0", port=PORT)
