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
import logging
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime

from flask import Flask, jsonify, request

app = Flask(__name__)


class _QuietPolls(logging.Filter):
    """Drop the dashboard's own polling from the access log.

    The dashboard refreshes every 10 s, so without this the add-on log — the
    one thing you open when a label did not come out — is 99% its own GETs,
    and the [print] and [geometry] lines that explain anything scroll away.
    Real work (POST /print, /selftest) still gets logged.
    """

    NOISE = ("GET /api/state", "GET /api/log", "GET / HTTP")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(n in message for n in self.NOISE)


logging.getLogger("werkzeug").addFilter(_QuietPolls())

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
#
# run.sh keeps rewriting this file after boot too (every 5s, printer-not-yet-
# seen only): a printer that powers up slower than the others after e.g. a
# mains outage gets picked up here on the next reload, with no add-on restart.
PRINTERS_JSON_PATH = "/data/printers.json"
CONFIGURED: list[dict] = []
_KNOWN_PRINTER_NAMES: set[str] = set()


def _reload_configured() -> list[dict]:
    """Re-read printers.json; geometry-fix/warm only entries new since last
    call (so an already-running Zebra never gets ^LS0 replayed at it, or a
    DYMO's PPD margins rewritten, on every timer tick)."""
    global CONFIGURED
    try:
        with open(PRINTERS_JSON_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return CONFIGURED
    CONFIGURED = data
    fresh = [e for e in data if e.get("name") not in _KNOWN_PRINTER_NAMES]
    if not fresh:
        return CONFIGURED
    _KNOWN_PRINTER_NAMES.update(e["name"] for e in fresh if e.get("name"))
    _normalize_dymo_ppds(fresh)
    _reset_zebra_geometry(fresh)
    _warm_native_px(fresh)
    for entry in fresh:
        print(f"[detect] {entry.get('name')} ({entry.get('kind')}) "
              "geregistreerd", flush=True)
    return CONFIGURED


def _reload_configured_loop() -> None:
    """Poll printers.json for entries run.sh added since the last read.
    5s (matching run.sh's own rescan cadence) keeps a newly detected printer
    showing up quickly without the read itself costing anything real."""
    import time as _time
    while True:
        _time.sleep(5)
        try:
            _reload_configured()
        except Exception as err:  # noqa: BLE001 - background loop must not die
            print(f"[detect] reload failed: {err}", flush=True)

RAW_FORMATS = ("zpl", "epl", "raw")
MAX_COPIES = 20

# Zebra Technologies USB vendor id (all ZD/GK/GX models), for the direct
# ~HS status query that CUPS cannot do for us.
ZEBRA_VID = 0x0A5F
# DYMO Corporation USB vendor id (all LabelWriter models).
DYMO_VID = 0x0922
_VID_BY_KIND = {"dymo": DYMO_VID, "zebra": ZEBRA_VID}

_USB_PRESENT_TTL = 4.0
_usb_present_cache: dict[int, tuple[float, bool]] = {}


def _usb_present(vid: int) -> bool:
    """Whether a USB device with this vendor id is on the bus right now.

    A CUPS queue stays defined after its printer is unplugged (lpstat -p
    keeps saying it exists), so "connected" must check live hardware, not
    just queue existence — otherwise a printer that browned out during a
    power outage would still read as connected until someone printed to it.
    """
    import time as _time
    now = _time.monotonic()
    cached = _usb_present_cache.get(vid)
    if cached and now - cached[0] < _USB_PRESENT_TTL:
        return cached[1]
    present = bool(re.search(rf"(?i)ID {vid:04x}:", _out(_run(["lsusb"]))))
    _usb_present_cache[vid] = (now, present)
    return present

# Print-head resolution per hardware family. Rendering clients use this (via
# GET /printers) to produce pixel-perfect art instead of relying on scaling.
DPI_BY_KIND = {"dymo": 300, "zebra": 203}

# Bump when the JSON contract of /printers//health//print changes shape, so
# clients (which update on their own schedule) can feature-detect.
API_VERSION = 1

# What this server.py is; it live-reloads from /share independently of the
# add-on version in config.yaml, so the dashboard shows both.
SERVER_VERSION = "0.11.0"

# Lets the dashboard show the real add-on log (Supervisor owns it, we don't).
# Present only when config.yaml grants hassio_api.
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


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
    kind = cfg.get("kind", "unknown")
    media = _media_of(name) or cfg.get("media", "")
    raster = _has_driver(name)
    accepts = ["png", "pdf", "jpg"] if raster else []
    # Zebra speaks ZPL natively; we hand raw jobs straight to the device.
    if kind == "zebra":
        accepts.append("zpl")
    dpi = DPI_BY_KIND.get(kind, 300)
    supported = _supported_media(name)
    if media and media not in supported:
        supported.insert(0, media)
    # The loaded label gets the measured (cached) size: it is the one that
    # actually prints, so its native_px must be exact, not just close.
    loaded = _media_entry(media, dpi, name, measured=True)
    if default_name is None:
        default_name = _default_queue()
    policy_mode, policy_align = _size_policy(name)
    vid = _VID_BY_KIND.get(kind)
    connected = _queue_exists(name) and (vid is None or _usb_present(vid))
    return {
        "name": name,
        "kind": kind,
        "model": cfg.get("model") or (MODEL if name == DEFAULT_PRINTER else ""),
        "connected": connected,
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


# --------------------------------------------------------------------------
# Zebra host status (~HS) — the only way to know a Zebra is out of labels
# --------------------------------------------------------------------------
# A Zebra swallows raw ZPL into its own buffer, so CUPS always reports
# success. ~HS makes the printer answer three comma-separated strings; in the
# first one field 2 is "paper out" and field 3 is "pause" (ZPL II manual).
# Reading the reply means talking to the device outside CUPS: the kernel's
# usblp node when it exists, else libusb via ctypes (no extra packages).
_ZEBRA_HS_TTL = 4.0
_hs_cache: dict[str, tuple[float, dict]] = {}


def _usb_lp_nodes() -> list[str]:
    try:
        return sorted(f"/dev/usb/{n}" for n in os.listdir("/dev/usb")
                      if n.startswith("lp"))
    except OSError:
        return []


def _parse_hs(reply: str) -> dict:
    """Decode a ~HS reply into the flags we care about."""
    lines = [ln.strip("\x02\x03\r\n ") for ln in reply.splitlines() if ln.strip()]
    if not lines:
        return {}
    fields = lines[0].split(",")
    if len(fields) < 5:
        return {}
    # String 1 is aaa,b,c,dddd,eee,...: b = paper out, c = pause, eee = the
    # number of formats sitting in the receive buffer. Both c and eee were
    # confirmed against this ZD220 on 2026-07-28: pulling the roll flipped c
    # to 1 (the printer pauses instead of raising paper-out), and each print
    # sent while empty incremented eee — those buffered labels do not exist
    # on paper yet.
    try:
        buffered = int(fields[4].strip() or 0)
    except ValueError:
        buffered = 0
    return {
        "paper_out": fields[1].strip() == "1",
        "paused": fields[2].strip() == "1",
        "buffered": buffered,
        "raw": " | ".join(lines)[:200],
    }


def _zebra_hs_via_node(path: str) -> dict:
    """Write ~HS to a usblp node and read the reply."""
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(fd, b"~HS\n")
        import select
        import time as _time
        deadline = _time.time() + 1.5
        buf = b""
        while _time.time() < deadline and buf.count(b"\n") < 3:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            try:
                chunk = os.read(fd, 512)
            except BlockingIOError:
                continue
            if not chunk:
                break
            buf += chunk
        return _parse_hs(buf.decode("ascii", errors="replace"))
    finally:
        os.close(fd)


def _zebra_host_status(printer: str) -> dict:
    """Ask a Zebra how it's doing; {} when we can't reach it."""
    import time as _time
    cached = _hs_cache.get(printer)
    now = _time.monotonic()
    if cached and now - cached[0] < _ZEBRA_HS_TTL:
        return cached[1]
    result: dict = {}
    for node in _usb_lp_nodes():
        try:
            result = _zebra_hs_via_node(node)
        except OSError:
            continue
        if result:
            result["via"] = node
            break
    if not result:
        result = _zebra_hs_via_libusb()
    _hs_cache[printer] = (now, result)
    return result


def _zebra_hs_via_libusb() -> dict:
    """~HS over pyusb, for when no usblp node exists.

    CUPS' usb backend only holds the device while a job runs, so between
    jobs the interface is free to claim. (A hand-rolled ctypes binding was
    tried first and segfaulted the service — pointer args need full argtype
    declarations; pyusb does that properly.)
    """
    try:
        import usb.core
        import usb.util
    except ImportError:
        return {"error": "pyusb_missing"}
    try:
        dev = usb.core.find(idVendor=ZEBRA_VID)
        if dev is None:
            return {"error": "no_zebra_on_usb"}
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        if ep_out is None or ep_in is None:
            return {"error": "no_bulk_endpoints"}
        ep_out.write(b"~HS\n", timeout=1000)
        chunks = []
        for _ in range(3):
            try:
                chunks.append(bytes(ep_in.read(512, timeout=1200)))
            except usb.core.USBError:
                break
        usb.util.dispose_resources(dev)
        parsed = _parse_hs(b"".join(chunks).decode("ascii", errors="replace"))
        if parsed:
            parsed["via"] = "pyusb"
            return parsed
        return {"error": "no_reply"}
    except Exception as err:  # noqa: BLE001 - status must never break printing
        return {"error": f"{type(err).__name__}: {err}"[:120]}


# --------------------------------------------------------------------------
# Attention: out of labels, jammed, stuck job
# --------------------------------------------------------------------------
# Measured on the real hardware (2026-07-28), because the two families fail
# very differently:
#   DYMO LW400  — CUPS keeps the job queued and the queue reports
#                 "com.dymo.out-of-paper-error"; reloading the roll prints it.
#   Zebra ZD220 — raw ZPL goes into the printer's own buffer, so CUPS marks
#                 the job COMPLETE and reports nothing at all; the label only
#                 appears after a roll + a FEED press. Only the printer itself
#                 knows, via a ~HS status query (see _zebra_host_status).
# Anything here is worth telling the user about: a label they think they
# printed does not exist yet.
ALERT_WORDS = ("out-of-paper", "media-empty", "media-needed", "media-jam",
               "cover-open", "door-open", "out-of-ink", "offline",
               "media-low", "marker-supply-empty")

# A job older than this while the queue is not actively printing means it is
# waiting for something physical (labels, a jam), not just busy.
STUCK_JOB_SECONDS = 25


def _queue_alerts(name: str) -> list[str]:
    """Alert/state-reason words CUPS reports for a queue."""
    text = _out(_run(["lpstat", "-l", "-p", name]))
    found = []
    for line in text.splitlines():
        low = line.lower()
        if "alerts:" not in low:
            continue
        value = line.split(":", 1)[1].strip()
        if not value or value.lower() in ("none", "job-printing"):
            continue
        for word in ALERT_WORDS:
            if word in value.lower() and value not in found:
                found.append(value)
    return found


def _pending_jobs() -> dict[str, dict]:
    """Per printer: how many jobs wait and how old the oldest one is."""
    import datetime
    import time as _time
    out: dict[str, dict] = {}
    now = _time.time()
    for line in _out(_run(["lpstat", "-o"])).splitlines():
        parts = line.split()
        if len(parts) < 4 or "-" not in parts[0]:
            continue
        queue = parts[0].rsplit("-", 1)[0]
        # "dymo-3 root 27648 Tue Jul 28 11:11:49 2026"
        stamp = " ".join(parts[3:])
        age = None
        for fmt in ("%a %d %b %Y %I:%M:%S %p %Z", "%a %b %d %H:%M:%S %Y"):
            try:
                age = int(now - datetime.datetime.strptime(stamp, fmt).timestamp())
                break
            except (ValueError, TypeError):
                continue
        if age is None:
            age = STUCK_JOB_SECONDS + 1  # unparsable = assume it's waiting
        entry = out.setdefault(queue, {"count": 0, "age": 0})
        entry["count"] += 1
        entry["age"] = max(entry["age"], age)
    return out


# How a printer is named to a human, and what CUPS jargon actually means.
# A notification must say what happened AND what to do — "com.dymo.
# out-of-paper-error" helps nobody at the fridge with a label in hand.
_KIND_LABEL = {"dymo": "DYMO", "zebra": "Zebra"}
_ALERT_MEANING = (
    (("out-of-paper", "media-empty", "media-needed", "marker-supply-empty"),
     "media_out", "Labels op in de {p}."),
    (("media-jam",), "media_jam", "Er zit een label vast in de {p}."),
    (("cover-open", "door-open"), "cover_open", "De klep van de {p} staat open."),
    (("media-low",), "media_low", "De {p} heeft bijna geen labels meer."),
    (("offline",), "offline", "De {p} is offline."),
)


def _printer_label(name: str) -> str:
    kind = _configured(name).get("kind") or name
    return _KIND_LABEL.get(kind, name)


def _waiting_phrase(pending: dict | None) -> str:
    n = (pending or {}).get("count") or 0
    if n == 1:
        return " Er wacht 1 label."
    if n > 1:
        return f" Er wachten {n} labels."
    return ""


def _reload_hint(kind: str) -> str:
    # Measured on this hardware: a DYMO resumes on its own once the roll is
    # back; a Zebra only releases its buffered label after a FEED press.
    if kind == "zebra":
        return (" Nieuwe rol erin, klep dicht en één keer op FEED drukken — "
                "dan komt het wachtende label eruit.")
    return " Nieuwe rol erin, dan print hij vanzelf verder."


def _attention() -> list[dict]:
    """Everything that needs a human: out of labels, jam, stuck job."""
    items: list[dict] = []
    pending = _pending_jobs()
    for name in _queues():
        kind = _configured(name).get("kind") or name
        label = _printer_label(name)
        alerts = _queue_alerts(name)
        if alerts:
            joined = "; ".join(alerts).lower()
            reason, text = "printer_alert", "De {p} vraagt aandacht."
            for words, key, template in _ALERT_MEANING:
                if any(w in joined for w in words):
                    reason, text = key, template
                    break
            message = text.format(p=label) + _waiting_phrase(pending.get(name))
            if reason == "media_out":
                message += _reload_hint(kind)
            items.append({
                "printer": name, "kind": kind, "reason": reason,
                "detail": "; ".join(alerts), "message": message,
            })
            continue
        job = pending.get(name) or {}
        age = job.get("age", 0)
        if age >= STUCK_JOB_SECONDS:
            mins = max(1, round(age / 60))
            items.append({
                "printer": name, "kind": kind, "reason": "job_stuck",
                "detail": f"oudste job wacht {age}s",
                "message": (f"De {label} print al {mins} minuten niet."
                            + _waiting_phrase(job)
                            + " Labels op of vastgelopen?"),
            })
    # The Zebra never surfaces through CUPS; ask the printer itself.
    for entry in CONFIGURED:
        if entry.get("kind") != "zebra":
            continue
        name = entry.get("name", "")
        if not name or any(i["printer"] == name for i in items):
            continue
        label = _printer_label(name)
        hs = _zebra_host_status(name)
        if not (hs.get("paper_out") or hs.get("paused")):
            continue
        buffered = hs.get("buffered") or 0
        if buffered == 1:
            waiting = " Er staat 1 label in het geheugen van de printer."
        elif buffered > 1:
            waiting = (f" Er staan {buffered} labels in het geheugen van de "
                       "printer.")
        else:
            waiting = ""
        # paper_out is the unambiguous one; a pause on this model means the
        # same thing in practice (measured), but a human may also have hit
        # the button — so the wording covers both without crying wolf.
        headline = (f"Labels op in de {label}." if hs.get("paper_out")
                    else f"De {label} print niet — labels op of op pauze.")
        items.append({
            "printer": name, "kind": "zebra",
            "reason": "media_out" if hs.get("paper_out") else "paused",
            "detail": hs.get("raw", "~HS"),
            "buffered": buffered,
            "message": headline + waiting + _reload_hint("zebra"),
        })
    return items


def _status() -> dict:
    printers = _printer_list()
    attention = _attention()
    return {
        "api_version": API_VERSION,
        # Kept for backwards compatibility with callers written against v0.1.
        "printer": _default_queue(),
        "model": MODEL,
        "connected": any(p["connected"] for p in printers),
        "default_media": DEFAULT_MEDIA,
        "printers": printers,
        # What needs a human right now (empty list = all good). Home Assistant
        # polls this and pushes a notification.
        "attention": attention,
        "needs_attention": bool(attention),
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
    _stats_bump(str(entry.get("printer") or "?"), bool(entry.get("ok")))
    _stats_save()
    # Only labels that actually came out consume roll.
    if entry.get("ok"):
        try:
            copies = max(1, int(entry.get("copies") or 1))
        except (TypeError, ValueError):
            copies = 1
        _roll_bump(str(entry.get("printer") or "?"), copies)


# --------------------------------------------------------------------------
# Daily counters: how much each printer really printed, per day
# --------------------------------------------------------------------------
# The journal is a 100-entry ring, so it cannot answer "how many labels did
# the DYMO print today" on any day busier than that, and it forgets the week
# before. These counters are two integers per printer per day, 60 days deep —
# small enough to keep forever, exact enough to plot.
STATS_PATH = "/data/print_stats.json"
STATS_DAYS = 60
_STATS: dict[str, dict[str, dict[str, int]]] = {}


def _stats_bump(printer: str, ok: bool, day: str = "") -> None:
    day = day or datetime.now().strftime("%Y-%m-%d")
    row = _STATS.setdefault(day, {}).setdefault(printer, {"ok": 0, "fail": 0})
    row["ok" if ok else "fail"] += 1
    for old in sorted(_STATS)[:-STATS_DAYS]:
        del _STATS[old]


def _stats_save() -> None:
    try:
        with open(STATS_PATH, "w") as f:
            json.dump(_STATS, f)
    except OSError:
        pass


def _stats_load() -> None:
    """Load the counters, seeding them from the journal on first run."""
    try:
        with open(STATS_PATH) as f:
            _STATS.update(json.load(f))
        return
    except (OSError, ValueError):
        pass
    # First boot after this upgrade: the journal still holds real history, so
    # seed from it and today's tile is right immediately instead of at zero.
    # Only ever runs once — after this the file exists and jobs increment it.
    for e in _JOURNAL:
        day = str(e.get("time", ""))[:10]
        if len(day) == 10:
            _stats_bump(str(e.get("printer") or "?"), bool(e.get("ok")), day)
    _stats_save()


def _stats_series(days: int = 14) -> list[dict]:
    """The last N days, oldest first, ready to plot."""
    import datetime as _dt
    today = _dt.date.today()
    out = []
    for back in range(days - 1, -1, -1):
        day = (today - _dt.timedelta(days=back)).isoformat()
        row = _STATS.get(day, {})
        out.append({
            "day": day,
            "per_printer": {p: v["ok"] for p, v in row.items() if v.get("ok")},
            "ok": sum(v.get("ok", 0) for v in row.values()),
            "fail": sum(v.get("fail", 0) for v in row.values()),
        })
    return out


def _stats_today() -> dict:
    row = _STATS.get(datetime.now().strftime("%Y-%m-%d"), {})
    return {
        # Labels that actually came out. Failures live in "fail" so the plot
        # and the "vandaag geprint" tile can never disagree about what a
        # printed label is.
        "per_printer": {p: v["ok"] for p, v in row.items() if v.get("ok")},
        "ok": sum(v.get("ok", 0) for v in row.values()),
        "fail": sum(v.get("fail", 0) for v in row.values()),
    }


# --------------------------------------------------------------------------
# Roll gauge: a rough "how much is left on this roll" estimate
# --------------------------------------------------------------------------
# Dashboard-only and deliberately advisory: no entity, no notification, nothing
# hangs off it. It counts labels (copies, not jobs) since the last "nieuwe rol"
# press and holds that against the roll's stated capacity. No label printer
# reports its remaining roll, so an estimate that drifts by a few labels is the
# honest best case — which is why the UI says "±" and never "op".
#
# It is NOT the out-of-labels detection: that stays _attention(), which asks
# the hardware. This only ever gets ahead of it as a heads-up.
ROLL_PATH = "/data/roll_state.json"

# Labels on a full roll per DYMO part number (DYMO's published counts).
DYMO_ROLL_LABELS = {
    "99010": 130, "99012": 260, "99014": 220, "99015": 320,
    "11352": 500, "11354": 1000, "99019": 150, "904980": 220,
}
# Zebra rolls vary by supplier far more than DYMO's do; 300 is typical for a
# 104x159 shipping roll, and it is editable in the UI anyway.
ZEBRA_ROLL_LABELS = 300

_ROLL: dict[str, dict] = {}


def _roll_default_capacity(printer: str) -> int:
    if _configured(printer).get("kind") == "zebra":
        return ZEBRA_ROLL_LABELS
    part = str(_addon_options().get("dymo_label", "")).split(" ")[0]
    return DYMO_ROLL_LABELS.get(part, 220)


def _roll_save() -> None:
    try:
        with open(ROLL_PATH, "w") as f:
            json.dump(_ROLL, f)
    except OSError:
        pass


def _roll_load() -> None:
    try:
        with open(ROLL_PATH) as f:
            _ROLL.update(json.load(f))
    except (OSError, ValueError):
        pass


def _roll_reset(printer: str, capacity: int | None = None) -> dict:
    old = _ROLL.get(printer) or {}
    cap = capacity or old.get("capacity") or _roll_default_capacity(printer)
    _ROLL[printer] = {
        "capacity": max(1, int(cap)),
        "used": 0,
        "since": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _roll_save()
    return _ROLL[printer]


def _roll_bump(printer: str, labels: int) -> None:
    entry = _ROLL.get(printer)
    if entry is None:
        # Never counted before: start the roll now rather than pretend it was
        # full at boot — an estimate that starts honest beats one that lies.
        entry = _roll_reset(printer)
    entry["used"] = int(entry.get("used", 0)) + max(0, labels)
    _roll_save()


def _roll_state(printer: str) -> dict:
    entry = _ROLL.get(printer) or {}
    cap = max(1, int(entry.get("capacity") or _roll_default_capacity(printer)))
    used = max(0, int(entry.get("used") or 0))
    left = max(0, cap - used)
    pct = round(100 * left / cap)
    return {
        "capacity": cap, "used": used, "left": left, "pct": pct,
        "level": "ok" if pct > 20 else ("low" if pct > 5 else "empty"),
        "since": entry.get("since", ""),
        "tracked": printer in _ROLL,
    }


# --------------------------------------------------------------------------
# The add-on log (Supervisor owns it)
# --------------------------------------------------------------------------
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _addon_log(lines: int = 200) -> dict:
    """Tail of this add-on's own log, via the Supervisor API."""
    if not SUPERVISOR_TOKEN:
        return {"ok": False,
                "error": "Geen Supervisor-toegang (hassio_api staat uit)."}
    import urllib.error
    import urllib.request
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    # Newer Supervisors answer ?lines=; older ones ignore it and send the lot,
    # so tail again on this side either way.
    for url in (f"http://supervisor/addons/self/logs?lines={lines}",
                "http://supervisor/addons/self/logs"):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as res:
                text = res.read().decode("utf-8", "replace")
            text = _ANSI.sub("", text)
            tail = text.splitlines()[-lines:]
            return {"ok": True, "text": "\n".join(tail), "lines": len(tail)}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
    return {"ok": False, "error": f"Log niet op te halen: {last}"}


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

    # Pre-flight for Zebras: they accept ZPL into their own buffer even with
    # no labels loaded, so without this the caller gets a cheerful "printed"
    # for a label that only exists in RAM. Measured 2026-07-28: after the
    # first failed feed the printer holds the format and blinks; the label
    # appears only after a new roll AND a FEED press.
    if _configured(printer).get("kind") == "zebra":
        hs = _zebra_host_status(printer)
        # This ZD220 signals "can't print" by pausing, not by raising the
        # paper-out flag, so both count as a stop.
        if hs.get("paper_out") or hs.get("paused"):
            notes.append("geweigerd: printer staat stil "
                         f"({'paper out' if hs.get('paper_out') else 'pauze'}, ~HS)")
            return {"ok": False, "error": "media_out", "printer": printer,
                    "detail": hs.get("raw", ""),
                    "buffered": hs.get("buffered", 0),
                    "hint": "De Zebra print nu niet — labels op of hij staat "
                            "op pauze. Nieuwe rol erin, klep dicht en één keer "
                            "op FEED drukken; stuur de job daarna opnieuw."}

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


@app.route("/attention", methods=["GET"])
def attention():
    """What needs a human right now — the endpoint Home Assistant polls."""
    items = _attention()
    return jsonify({
        "needs_attention": bool(items),
        "count": len(items),
        "items": items,
        # One ready-made line for a notification/announce.
        "message": items[0]["message"] if items else "",
    })


@app.route("/debug/usb", methods=["GET"])
def debug_usb():
    """Which route to the Zebra we have, and what it answers to ~HS."""
    zebras = [e.get("name") for e in CONFIGURED if e.get("kind") == "zebra"]
    out = {
        "usblp_nodes": _usb_lp_nodes(),
        "dev_bus_usb": _out(_run(["sh", "-c", "ls -l /dev/bus/usb/*/ 2>&1"]))[:600],
        "lsusb": _out(_run(["lsusb"])),
        "zebras": zebras,
    }
    for name in zebras:
        _hs_cache.pop(name, None)  # always measure fresh here
        out[f"host_status:{name}"] = _zebra_host_status(name)
    return jsonify(out)


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


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
# One self-contained page: no CDN, no build step, no external fonts — it has to
# work on a Pi with no internet and inside the Home Assistant ingress iframe.
# All URLs are relative and resolved against <base>, because under ingress the
# page lives at /api/hassio_ingress/<token>/ and absolute paths would escape it.
_DASH = r"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<base href="__BASE__">
<title>Label Printer</title>
<style>
:root{
  color-scheme:light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#008300; --s3:#e87ba4; --s4:#eda100;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b;
  --art-edge:rgba(11,11,11,.16); --art-shadow:rgba(11,11,11,.10);
  --wash:rgba(11,11,11,.04);
}
@media (prefers-color-scheme:dark){
  :root:where(:not([data-theme="light"])){
    color-scheme:dark;
    --plane:#0d0d0d; --surface:#1a1a19;
    --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#008300; --s3:#d55181; --s4:#c98500;
    --art-edge:rgba(255,255,255,.22); --art-shadow:rgba(0,0,0,.45);
    --wash:rgba(255,255,255,.05);
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#008300; --s3:#d55181; --s4:#c98500;
  --art-edge:rgba(255,255,255,.22); --art-shadow:rgba(0,0,0,.45);
  --wash:rgba(255,255,255,.05);
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 56px}
h1{font-size:21px;margin:0;letter-spacing:-.01em}
h2{font-size:15px;margin:0;font-weight:600}
h3{font-size:16px;margin:0;font-weight:600;letter-spacing:-.005em}
.ico{width:20px;height:20px;flex:none;fill:currentColor;display:block}
.ico-lg{width:26px;height:26px}
.ico-sm{width:15px;height:15px}

/* header */
.head{display:flex;align-items:center;justify-content:space-between;gap:16px;
  flex-wrap:wrap;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.brand-badge{width:44px;height:44px;border-radius:12px;flex:none;display:grid;
  place-items:center;background:var(--s1);color:#fff}
.sub{margin:2px 0 0;color:var(--ink2);font-size:13px}
.live{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;
  border:1px solid var(--border);border-radius:999px;background:var(--surface);
  font-size:13px;color:var(--ink2)}
.live .dot{width:8px;height:8px;border-radius:50%;background:var(--good);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--good) 22%,transparent)}
.live.off .dot{background:var(--crit);box-shadow:0 0 0 3px color-mix(in srgb,var(--crit) 22%,transparent)}

/* cards */
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:18px;margin-bottom:18px}
.card-h{display:flex;align-items:center;gap:9px;margin-bottom:14px}
.card-h .ico{color:var(--muted)}
.card-note{margin-left:auto;color:var(--muted);font-size:12.5px}

/* alerts */
.alert{display:flex;gap:12px;align-items:flex-start;padding:14px 16px;
  border-radius:14px;margin-bottom:18px;border:1px solid;font-size:14px}
.alert .ico{margin-top:1px}
.alert.warn{background:color-mix(in srgb,var(--warn) 13%,var(--surface));
  border-color:color-mix(in srgb,var(--warn) 45%,transparent)}
.alert.crit{background:color-mix(in srgb,var(--crit) 11%,var(--surface));
  border-color:color-mix(in srgb,var(--crit) 42%,transparent)}
.alert b{display:block;margin-bottom:2px}
.alert span{color:var(--ink2)}

/* stat tiles */
.tiles{display:grid;gap:12px;margin-bottom:18px;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:16px 18px;display:flex;flex-direction:column;gap:2px}
.tile .tl{font-size:12.5px;color:var(--ink2)}
.tile .tv{font-size:26px;font-weight:600;letter-spacing:-.02em;line-height:1.25}
.tile .ts{font-size:12px;color:var(--muted)}
.tile-hero{grid-column:span 2}
.tile-hero .hero{font-size:52px;font-weight:600;letter-spacing:-.03em;line-height:1.05}
@media(max-width:520px){.tile-hero{grid-column:span 2}}

/* printer cards */
.printers{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));
  margin-bottom:18px}
@media(max-width:520px){.printers{grid-template-columns:1fr}}
.pcard{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:18px;display:flex;gap:16px}
@media(max-width:640px){.pcard{flex-direction:column}}
.art{width:170px;flex:none;align-self:flex-start}
@media(max-width:640px){.art{width:190px;align-self:center}}
.art svg{width:100%;height:auto;display:block}
.art .edge{stroke:var(--art-edge);stroke-width:1.4;stroke-linejoin:round}
.pbody{flex:1;min-width:0;display:flex;flex-direction:column;gap:12px}
.ptop{display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap}
.ptop h3{flex:1;min-width:0}
.qname{color:var(--muted);font-size:12.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.badge{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.02em;
  padding:2px 8px;border-radius:999px;background:var(--wash);color:var(--ink2);
  vertical-align:2px;margin-left:6px}
.status{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;
  padding:4px 10px;border-radius:999px;white-space:nowrap}
.status.good{color:var(--good);background:color-mix(in srgb,var(--good) 13%,transparent)}
.status.warn{color:#8a5a00;background:color-mix(in srgb,var(--warn) 22%,transparent)}
.status.crit{color:var(--crit);background:color-mix(in srgb,var(--crit) 13%,transparent)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .status.warn{color:var(--warn)}}
:root[data-theme="dark"] .status.warn{color:var(--warn)}
.specs{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:10px 14px;margin:0}
.specs div{min-width:0}
.specs dt{font-size:11.5px;color:var(--muted);margin-bottom:1px}
.specs dd{margin:0;font-size:13.5px;font-weight:500;overflow-wrap:anywhere}

/* roll meter */
.roll{border:1px solid var(--border);border-radius:12px;padding:12px 13px;
  background:var(--wash)}
.roll-h{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
  font-size:13px;margin-bottom:8px}
.roll-h b{font-weight:600}
.roll-pct{font-variant-numeric:tabular-nums;color:var(--ink2);font-size:12.5px}
.meter{height:8px;border-radius:999px;overflow:hidden;
  background:color-mix(in srgb,var(--fill) 20%,transparent)}
.meter i{display:block;height:100%;border-radius:999px;background:var(--fill);
  transition:width .35s ease}
.roll.ok{--fill:var(--s1)} .roll.low{--fill:var(--warn)} .roll.empty{--fill:var(--crit)}
.roll-f{display:flex;align-items:center;justify-content:space-between;gap:10px;
  flex-wrap:wrap;margin-top:9px;font-size:12px;color:var(--muted)}
.roll-actions{display:flex;gap:6px}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:13px;
  font-weight:500;padding:6px 12px;border-radius:9px;cursor:pointer;
  border:1px solid var(--border);background:var(--surface);color:var(--ink)}
.btn:hover{background:var(--wash)}
.btn:disabled{opacity:.5;cursor:default}
.btn-sm{font-size:12px;padding:5px 10px}
.btn-pri{background:var(--s1);border-color:transparent;color:#fff}
.btn-pri:hover{filter:brightness(1.08);background:var(--s1)}

/* chart */
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:6px}
.key{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink2)}
.key i{width:10px;height:10px;border-radius:3px;flex:none}
.chartwrap{position:relative}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.chart text{font:11px system-ui,-apple-system,sans-serif;fill:var(--muted);
  font-variant-numeric:tabular-nums}
.chart .cap{fill:var(--ink2);font-weight:600}
.chart .today{fill:var(--ink2);font-weight:600}
.tip{position:absolute;pointer-events:none;z-index:5;background:var(--surface);
  border:1px solid var(--border);border-radius:10px;padding:9px 11px;font-size:12.5px;
  box-shadow:0 6px 24px var(--art-shadow);min-width:132px;transform:translate(-50%,-100%)}
.tip .th{font-weight:600;margin-bottom:5px}
.tip .tr{display:flex;align-items:center;gap:7px;justify-content:space-between}
.tip .tr i{width:9px;height:9px;border-radius:2px;flex:none}
.tip .tr span{color:var(--ink2)}
.tip .tr b{font-variant-numeric:tabular-nums}
.empty{padding:26px 0;text-align:center;color:var(--muted);font-size:13.5px}

/* tables */
details.tv{margin-top:12px}
details.tv summary{cursor:pointer;font-size:12.5px;color:var(--ink2);
  list-style:none;display:inline-flex;align-items:center;gap:6px}
details.tv summary::-webkit-details-marker{display:none}
.tablewrap{overflow-x:auto;margin:0 -18px;padding:0 18px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:600;font-size:11.5px;color:var(--muted);
  padding:0 12px 8px 0;white-space:nowrap;letter-spacing:.02em}
td{padding:9px 12px 9px 0;border-top:1px solid var(--grid);vertical-align:top}
td.t{white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px}
td.num{text-align:right;font-variant-numeric:tabular-nums;padding-right:0}
.chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:500;
  padding:3px 9px;border-radius:999px;white-space:nowrap}
.chip.good{color:var(--good);background:color-mix(in srgb,var(--good) 13%,transparent)}
.chip.bad{color:var(--crit);background:color-mix(in srgb,var(--crit) 13%,transparent)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.detail{display:block;color:var(--muted);font-size:11.5px;margin-top:3px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.fchip{font:inherit;font-size:12.5px;padding:5px 11px;border-radius:999px;cursor:pointer;
  border:1px solid var(--border);background:transparent;color:var(--ink2)}
.fchip[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);
  color:var(--surface);font-weight:500}

/* log */
.log{margin:0;padding:14px;border-radius:12px;background:var(--wash);
  border:1px solid var(--border);max-height:340px;overflow:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
  line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ink2)}
pre.api{margin:0;padding:13px;border-radius:11px;background:var(--wash);
  border:1px solid var(--border);overflow-x:auto;font-size:12px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink2)}
.api h4{margin:14px 0 6px;font-size:12.5px;color:var(--ink2);font-weight:600}
.api h4:first-of-type{margin-top:0}

/* toast */
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(10px);
  background:var(--ink);color:var(--plane);padding:11px 18px;border-radius:11px;
  font-size:13.5px;box-shadow:0 8px 30px var(--art-shadow);opacity:0;
  transition:opacity .2s,transform .2s;pointer-events:none;z-index:50;max-width:88vw}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>
<div class="wrap">
  <header class="head">
    <div class="brand">
      <span class="brand-badge" id="brand-ico"></span>
      <div>
        <h1>Label Printer</h1>
        <p class="sub" id="sub">laden…</p>
      </div>
    </div>
    <span class="live" id="live"><i class="dot"></i><span id="live-t">verbinden…</span></span>
  </header>

  <div id="alerts"></div>

  <section class="tiles">
    <div class="tile tile-hero">
      <span class="tl">Vandaag geprint</span>
      <span class="hero" id="t-today">–</span>
      <span class="ts" id="t-today-sub">labels</span>
    </div>
    <div class="tile"><span class="tl">Mislukt</span><span class="tv" id="t-fail">–</span><span class="ts">vandaag</span></div>
    <div class="tile"><span class="tl">Laatste 7 dagen</span><span class="tv" id="t-week">–</span><span class="ts">labels geprint</span></div>
    <div class="tile"><span class="tl">Gemiddeld</span><span class="tv" id="t-avg">–</span><span class="ts">labels per dag</span></div>
  </section>

  <section class="printers" id="printers"></section>

  <section class="card">
    <div class="card-h"><span id="i-chart"></span><h2>Printvolume</h2>
      <span class="card-note">laatste 14 dagen</span></div>
    <div class="legend" id="legend"></div>
    <div class="chartwrap">
      <div class="chart" id="chart"></div>
      <div class="tip" id="tip" hidden></div>
    </div>
    <details class="tv"><summary id="tv-sum">Tabelweergave</summary>
      <div class="tablewrap" style="margin-top:10px"><table>
        <thead><tr><th>Dag</th><th class="num">Gelukt</th><th class="num">Mislukt</th><th class="num">Totaal</th></tr></thead>
        <tbody id="cbody"></tbody></table></div>
    </details>
  </section>

  <section class="card">
    <div class="card-h"><span id="i-hist"></span><h2>Printjobs</h2>
      <span class="card-note" id="jnote"></span></div>
    <div class="filters" id="filters"></div>
    <div class="tablewrap"><table>
      <thead><tr><th>Tijd</th><th>Printer</th><th>Formaat</th><th></th><th>Wat is er gebeurd</th></tr></thead>
      <tbody id="jbody"></tbody></table></div>
  </section>

  <section class="card">
    <div class="card-h"><span id="i-log"></span><h2>Add-on log</h2>
      <button class="btn btn-sm" id="logbtn" style="margin-left:auto">Ververs</button></div>
    <pre class="log" id="log">laden…</pre>
  </section>

  <section class="card api">
    <div class="card-h"><span id="i-api"></span><h2>API</h2>
      <span class="card-note" id="apihost"></span></div>
    <h4>PNG/PDF printen</h4>
    <pre class="api">curl -F file=@label.png -F printer=dymo http://HOST:8000/print</pre>
    <h4>Raw ZPL printen</h4>
    <pre class="api">curl -H 'Content-Type: application/json' \
  -d '{"printer":"zebra","zpl":"^XA^FO50,50^A0N,50,50^FDHi^FS^XZ"}' \
  http://HOST:8000/print</pre>
    <h4>Printers opvragen / testen</h4>
    <pre class="api">curl http://HOST:8000/printers
curl -X POST 'http://HOST:8000/selftest?printer=zebra'</pre>
  </section>
</div>
<div id="toast"></div>

<script>
const $ = (s) => document.querySelector(s);
const nl = new Intl.NumberFormat('nl-NL');
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const P = {
  printer:'M18,3H6V7H18M19,12A1,1 0 0,1 18,11A1,1 0 0,1 19,10A1,1 0 0,1 20,11A1,1 0 0,1 19,12M16,19H8V14H16M19,8H5A3,3 0 0,0 2,11V17H6V21H18V17H22V11A3,3 0 0,0 19,8Z',
  alert:'M13,14H11V9H13M13,18H11V16H13M1,21H23L12,2L1,21Z',
  check:'M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22A10,10 0 0,1 2,12A10,10 0 0,1 12,2M11,16.5L18,9.5L16.59,8.09L11,13.67L7.91,10.59L6.5,12L11,16.5Z',
  close:'M12,2C17.53,2 22,6.47 22,12C22,17.53 17.53,22 12,22C6.47,22 2,17.53 2,12C2,6.47 6.47,2 12,2M15.59,7L12,10.59L8.41,7L7,8.41L10.59,12L7,15.59L8.41,17L12,13.41L15.59,17L17,15.59L13.41,12L17,8.41L15.59,7Z',
  chart:'M22,21H2V3H4V19H6V10H10V19H12V6H16V19H18V14H22V21Z',
  hist:'M11,7V12.11L15.71,14.9L16.5,13.62L12.5,11.25V7M12.5,2C8.97,2 5.91,3.92 4.27,6.77L2,4.5V11H8.5L5.75,8.25C6.96,5.73 9.5,4 12.5,4A7.5,7.5 0 0,1 20,11.5A7.5,7.5 0 0,1 12.5,19C9.23,19 6.47,16.91 5.44,14H3.34C4.44,18.03 8.11,21 12.5,21A9.5,9.5 0 0,0 22,11.5A9.5,9.5 0 0,0 12.5,2Z',
  log:'M20,19V7H4V19H20M20,3A2,2 0 0,1 22,5V19A2,2 0 0,1 20,21H4A2,2 0 0,1 2,19V5C2,3.89 2.9,3 4,3H20M13,17V15H18V17H13M9.58,13L5.57,9H8.4L11.7,12.3C12.09,12.69 12.09,13.33 11.7,13.72L8.42,17H5.59L9.58,13Z',
  api:'M8.5,13.5L11,16.5L14.5,12L19,18H5M21,19V5C21,3.89 20.1,3 19,3H5A2,2 0 0,0 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19Z',
  test:'M6,22A3,3 0 0,1 3,19C3,18.4 3.18,17.84 3.5,17.37L9,7.81V6A1,1 0 0,1 8,5V4A2,2 0 0,1 10,2H14A2,2 0 0,1 16,4V5A1,1 0 0,1 15,6V7.81L20.5,17.37C20.82,17.84 21,18.4 21,19A3,3 0 0,1 18,22H6M5,19A1,1 0 0,0 6,20H18A1,1 0 0,0 19,19C19,18.79 18.93,18.59 18.82,18.43L16.53,14.47L14,17L8.93,11.93L5.18,18.43C5.07,18.59 5,18.79 5,19Z',
  roll:'M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M12,6A6,6 0 0,1 18,12A6,6 0 0,1 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6M12,10A2,2 0 0,0 10,12A2,2 0 0,0 12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10Z',
};
const icon = (d, cls) => '<svg class="ico ' + (cls||'') + '" viewBox="0 0 24 24" aria-hidden="true"><path d="' + d + '"/></svg>';

/* ---- printer illustrations -------------------------------------------- */
/* Drawn to match the real hardware: the DYMO is the light-grey wedge with a
   dark sloping lid, the Zebra the black box. The LED carries live status, so
   the picture is not decoration — it is the status light you'd look at. */
const LED = {good:'#0ca30c', warn:'#fab219', crit:'#d03b3b'};

function labelArt(x, y, w, h, bars) {
  let s = '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h +
    '" rx="4" fill="#fff" class="edge"/>';
  s += '<rect x="' + (x+9) + '" y="' + (y+10) + '" width="' + (w-30) + '" height="4" rx="2" fill="#c7c5bd"/>';
  s += '<rect x="' + (x+9) + '" y="' + (y+19) + '" width="' + (w-18) + '" height="3" rx="1.5" fill="#dedbd2"/>';
  s += '<rect x="' + (x+9) + '" y="' + (y+26) + '" width="' + (w-40) + '" height="3" rx="1.5" fill="#dedbd2"/>';
  const bw = [2,1,3,1,2,4,1,2,1,3,2,1];
  let bx = x + 9;
  for (let i = 0; i < bw.length && bx < x + w - 12; i++) {
    s += '<rect x="' + bx + '" y="' + (y+h-bars-6) + '" width="' + bw[i] +
      '" height="' + bars + '" fill="#57554e"/>';
    bx += bw[i] + 2;
  }
  return s;
}

function dymoArt(led) {
  return '<svg viewBox="0 0 260 176" role="img" aria-label="DYMO LabelWriter">' +
    '<ellipse cx="136" cy="152" rx="94" ry="9" fill="var(--art-shadow)"/>' +
    '<circle cx="168" cy="34" r="21" fill="#eceadf" class="edge"/>' +
    '<circle cx="168" cy="34" r="7" fill="#c9c6bd"/>' +
    '<path d="M52,74 L196,74 L224,46 L80,46 Z" fill="#3d3d3f" class="edge"/>' +
    '<path d="M196,74 L224,46 L224,110 L196,138 Z" fill="#a6a5a0" class="edge"/>' +
    '<rect x="52" y="74" width="144" height="64" rx="5" fill="#d7d6d1" class="edge"/>' +
    '<text x="92" y="64" font-family="system-ui,sans-serif" font-size="9"' +
      ' letter-spacing="1.6" fill="#a5a4a0">DYMO</text>' +
    '<rect x="82" y="70" width="92" height="7" rx="3.5" fill="#2a2a2b"/>' +
    labelArt(86, 76, 84, 66, 16) +
    '<circle cx="67" cy="120" r="9" fill="#bcbbb5" class="edge"/>' +
    '<circle cx="67" cy="120" r="8" fill="' + led + '" opacity=".2"/>' +
    '<circle cx="67" cy="120" r="4.5" fill="' + led + '"/>' +
    '</svg>';
}

function zebraArt(led) {
  return '<svg viewBox="0 0 260 176" role="img" aria-label="Zebra labelprinter">' +
    '<ellipse cx="136" cy="152" rx="94" ry="9" fill="var(--art-shadow)"/>' +
    '<path d="M48,68 L200,68 L228,40 L76,40 Z" fill="#3c3c38" class="edge"/>' +
    '<path d="M200,68 L228,40 L228,110 L200,138 Z" fill="#232321" class="edge"/>' +
    '<rect x="48" y="68" width="152" height="70" rx="5" fill="#2f2f2c" class="edge"/>' +
    '<text x="86" y="58" font-family="system-ui,sans-serif" font-size="9"' +
      ' letter-spacing="1.8" fill="#8f8e88">ZEBRA</text>' +
    '<circle cx="182" cy="52" r="8" fill="#1b1b19" class="edge"/>' +
    '<circle cx="182" cy="52" r="7.5" fill="' + led + '" opacity=".22"/>' +
    '<circle cx="182" cy="52" r="4" fill="' + led + '"/>' +
    '<rect x="70" y="64" width="108" height="7" rx="3.5" fill="#141413"/>' +
    labelArt(74, 70, 100, 72, 20) +
    '</svg>';
}

/* ---- state ------------------------------------------------------------- */
let S = null, LOGBUSY = false;
let F = {status:'all', printer:'all'};

function statusOf(p, attention) {
  const a = (attention || []).find((x) => x.printer === p.name);
  if (!p.connected) return {k:'crit', t:'Niet verbonden', i:P.close};
  if (a) {
    const t = a.reason === 'media_out' ? 'Labels op'
      : a.reason === 'media_jam' ? 'Vastgelopen'
      : a.reason === 'cover_open' ? 'Klep open'
      : a.reason === 'media_low' ? 'Bijna leeg'
      : a.reason === 'paused' ? 'Op pauze' : 'Vraagt aandacht';
    return {k:'warn', t:t, i:P.alert};
  }
  return {k:'good', t:'Klaar', i:P.check};
}

/* Fixed slot order — a series keeps its colour when another printer drops off
   the list, so colour follows the printer and never its rank. */
const SLOTS = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)'];
const seriesColor = (i) => SLOTS[i] || 'var(--muted)';

/* Every queue that appears anywhere in the window, not just the ones plugged
   in right now: a printer unplugged yesterday still owns labels on yesterday's
   bar, and the segments have to add up to the total the cap label states. */
function seriesKeys() {
  const keys = (S.printers || []).map((p) => p.name);
  for (const d of (S.series || [])) {
    for (const k of Object.keys(d.per_printer || {})) {
      if (!keys.includes(k)) keys.push(k);
    }
  }
  return keys.length ? keys : ['?'];
}

function render() {
  const st = S;
  const ps = st.printers || [];
  $('#sub').textContent = ps.length + (ps.length === 1 ? ' printer' : ' printers') +
    ' · server v' + st.server_version;

  /* alerts */
  $('#alerts').innerHTML = (st.attention || []).map((a) =>
    '<div class="alert ' + (a.reason === 'media_low' ? 'warn' : 'crit') + '">' +
    icon(P.alert) + '<div><b>' + esc(a.message.split('.')[0]) + '.</b>' +
    '<span>' + esc(a.message.split('.').slice(1).join('.').trim()) + '</span></div></div>').join('');

  /* tiles */
  const t = st.today || {ok:0, fail:0, per_printer:{}};
  $('#t-today').textContent = nl.format(t.ok);
  $('#t-fail').textContent = nl.format(t.fail);
  const per = Object.entries(t.per_printer || {}).filter((e) => e[1] > 0)
    .map((e) => e[1] + '× ' + e[0]).join(' · ');
  $('#t-today-sub').textContent = t.ok === 0 ? 'nog niks vandaag' : (per || 'labels');
  const week = (st.series || []).slice(-7);
  const wk = week.reduce((s, d) => s + d.ok, 0);
  $('#t-week').textContent = nl.format(wk);
  $('#t-avg').textContent = week.length
    ? (wk / week.length).toFixed(1).replace('.', ',') : '–';

  /* printers */
  $('#printers').innerHTML = ps.map((p, i) => {
    const s = statusOf(p, st.attention);
    const art = p.kind === 'zebra' ? zebraArt(LED[s.k]) : dymoArt(LED[s.k]);
    const r = p.roll || {};
    const today = (t.per_printer || {})[p.name] || 0;
    const px = (p.native_px || []).join(' × ');
    return '<article class="pcard">' +
      '<div class="art">' + art + '</div>' +
      '<div class="pbody">' +
        '<div class="ptop"><h3>' + esc(p.title || p.name) +
          (p.default ? '<span class="badge">standaard</span>' : '') +
          '<br><span class="qname">' + esc(p.name) + '</span></h3>' +
          '<span class="status ' + s.k + '">' + icon(s.i, 'ico-sm') + esc(s.t) + '</span></div>' +
        '<dl class="specs">' +
          '<div><dt>Geladen label</dt><dd>' + esc(p.label || '—') + '</dd></div>' +
          '<div><dt>Canvas</dt><dd>' + (px ? px + ' px' : '—') + ' @ ' + p.dpi + ' dpi</dd></div>' +
          '<div><dt>Accepteert</dt><dd>' + ((p.accepts || []).join(', ').toUpperCase() || '—') + '</dd></div>' +
          '<div><dt>Vandaag</dt><dd>' + nl.format(today) + (today === 1 ? ' label' : ' labels') + '</dd></div>' +
        '</dl>' +
        '<div class="roll ' + (r.level || 'ok') + '">' +
          '<div class="roll-h"><span><b>Rol</b> — ' + (r.tracked
            ? 'nog ±' + nl.format(r.left || 0) + ' van ' + nl.format(r.capacity || 0)
            : 'ingesteld op ' + nl.format(r.capacity || 0) + ' per rol') + '</span>' +
            '<span class="roll-pct">' + (r.tracked ? r.pct + '%' : '') + '</span></div>' +
          '<div class="meter"><i style="width:' + Math.max(2, r.pct || 0) + '%"></i></div>' +
          '<div class="roll-f"><span>' + (r.tracked
            ? nl.format(r.used || 0) + ' geprint sinds ' + esc(r.since)
            : 'nog niet bijgehouden — schatting start bij de eerste print') + '</span>' +
            '<span class="roll-actions">' +
              '<button class="btn btn-sm" data-roll="reset" data-p="' + esc(p.name) + '">Nieuwe rol</button>' +
              '<button class="btn btn-sm" data-roll="capacity" data-p="' + esc(p.name) +
                '" data-cap="' + (r.capacity || 0) + '">Aantal…</button>' +
            '</span></div>' +
        '</div>' +
        '<div><button class="btn btn-pri" data-test="' + esc(p.name) + '">' +
          icon(P.test, 'ico-sm') + 'Testprint</button></div>' +
      '</div></article>';
  }).join('') || '<div class="card empty">Geen printers gevonden. Zit de USB-kabel erin?</div>';

  renderChart();
  renderFilters();
  renderJournal();
}

/* ---- chart ------------------------------------------------------------- */
/* Smallest round axis that clears the peak in 3–5 whole steps, so every
   gridline lands on an integer number of labels. */
function axisFor(peak) {
  for (const s of [1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000]) {
    for (let n = 3; n <= 5; n++) {
      if (s * n >= peak) return {max: s * n, step: s};
    }
  }
  return {max: peak, step: Math.ceil(peak / 4)};
}

function renderChart() {
  const host = $('#chart');
  const days = (S.series || []);
  const keys = seriesKeys();

  $('#legend').innerHTML = keys.length > 1 ? keys.map((k, i) =>
    '<span class="key"><i style="background:' + seriesColor(i) + '"></i>' + esc(k) + '</span>').join('') : '';

  $('#cbody').innerHTML = days.map((d) =>
    '<tr><td class="t">' + esc(d.day) + '</td><td class="num">' + d.ok +
    '</td><td class="num">' + d.fail + '</td><td class="num">' + (d.ok + d.fail) + '</td></tr>').join('');
  $('#tv-sum').textContent = 'Tabelweergave (' + days.length + ' dagen)';

  // Printed labels only, so a bar's height always equals its stacked segments.
  const totals = days.map((d) => d.ok);
  const peak = Math.max(0, ...totals);
  if (peak === 0) {
    host.innerHTML = '<div class="empty">Nog niks geprint in de laatste ' +
      days.length + ' dagen.</div>';
    return;
  }

  const W = Math.max(320, host.clientWidth || 640);
  const H = 208, PL = 34, PR = 8, PT = 20, PB = 26;
  const iw = W - PL - PR, ih = H - PT - PB;
  const ax = axisFor(peak), max = ax.max;
  const band = iw / days.length;
  const bw = Math.min(24, band - 8);
  const y = (v) => PT + ih - (v / max) * ih;
  const GAP = 2;

  let g = '';
  for (let v = 0; v <= max; v += ax.step) {
    g += '<line x1="' + PL + '" y1="' + y(v) + '" x2="' + (W - PR) + '" y2="' + y(v) +
      '" stroke="' + (v === 0 ? 'var(--axis)' : 'var(--grid)') + '" stroke-width="1"/>';
    g += '<text x="' + (PL - 8) + '" y="' + (y(v) + 3.5) + '" text-anchor="end">' + v + '</text>';
  }

  const step = Math.ceil(days.length / 7);
  days.forEach((d, i) => {
    const x = PL + band * i + (band - bw) / 2;
    let base = y(0), first = true;
    keys.forEach((k, ki) => {
      const v = (d.per_printer || {})[k] || 0;
      if (!v) return;
      const top = base - (v / max) * ih;
      // 4px rounded data-end on the topmost segment only (the baseline end
      // stays square), and a 2px surface gap carved off the bottom of every
      // segment that sits on another one.
      const isTop = keys.slice(ki + 1).every((k2) => !((d.per_printer || {})[k2]));
      const hh = Math.max(1, base - (first ? 0 : GAP) - top);
      g += '<path d="' + barPath(x, top, bw, hh, isTop ? 4 : 0) +
        '" fill="' + seriesColor(ki) + '"/>';
      base = top;
      first = false;
    });
    const tot = totals[i];
    if (tot === peak && tot > 0) {
      g += '<text class="cap" x="' + (x + bw / 2) + '" y="' + (y(tot) - 7) +
        '" text-anchor="middle">' + tot + '</text>';
    }
    const isToday = i === days.length - 1;
    if (i % step === 0 || isToday) {
      const lbl = isToday ? 'vandaag' : String(Number(d.day.slice(8, 10)));
      g += '<text class="' + (isToday ? 'today' : '') + '" x="' + (x + bw / 2) +
        '" y="' + (H - 8) + '" text-anchor="middle">' + lbl + '</text>';
    }
    g += '<rect x="' + (PL + band * i) + '" y="' + PT + '" width="' + band +
      '" height="' + ih + '" fill="transparent" data-i="' + i + '"/>';
  });

  host.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W +
    '" height="' + H + '">' + g + '</svg>';

  // The SVG is drawn in viewBox units but laid out at the host's real width,
  // so every coordinate has to be scaled before it can position a DOM tooltip.
  const tip = $('#tip');
  const scale = (host.clientWidth || W) / W;
  host.querySelectorAll('rect[data-i]').forEach((r) => {
    r.addEventListener('mouseenter', () => {
      const i = +r.dataset.i, d = days[i];
      const rows = keys.map((k, ki) => {
        const v = (d.per_printer || {})[k] || 0;
        return '<div class="tr"><span><i style="background:' + seriesColor(ki) +
          ';display:inline-block"></i> ' + esc(k) + '</span><b>' + v + '</b></div>';
      }).join('');
      const dt = new Date(d.day + 'T00:00:00');
      tip.innerHTML = '<div class="th">' +
        dt.toLocaleDateString('nl-NL', {weekday:'short', day:'numeric', month:'short'}) +
        '</div>' + rows + (d.fail ? '<div class="tr"><span>waarvan mislukt</span><b>' +
        d.fail + '</b></div>' : '');
      tip.hidden = false;
      const bx = (PL + band * i + band / 2) * scale;
      const half = tip.offsetWidth / 2;
      tip.style.left = Math.min(Math.max(bx, half), host.clientWidth - half) + 'px';
      tip.style.top = (y(totals[i]) * scale - 8) + 'px';
    });
  });
}

function barPath(x, yy, w, h, r) {
  r = Math.min(r, w / 2, h);
  if (r <= 0) return 'M' + x + ',' + yy + ' h' + w + ' v' + h + ' h' + (-w) + ' Z';
  return 'M' + x + ',' + (yy + h) + ' L' + x + ',' + (yy + r) +
    ' Q' + x + ',' + yy + ' ' + (x + r) + ',' + yy +
    ' L' + (x + w - r) + ',' + yy + ' Q' + (x + w) + ',' + yy + ' ' + (x + w) + ',' + (yy + r) +
    ' L' + (x + w) + ',' + (yy + h) + ' Z';
}

/* ---- journal ----------------------------------------------------------- */
function renderFilters() {
  const names = (S.printers || []).map((p) => p.name);
  const chips = [['status', 'all', 'Alles'], ['status', 'ok', 'Gelukt'],
                 ['status', 'fail', 'Mislukt']];
  if (names.length > 1) {
    chips.push(['printer', 'all', 'Alle printers']);
    names.forEach((n) => chips.push(['printer', n, n]));
  }
  $('#filters').innerHTML = chips.map(([g, v, l]) =>
    '<button class="fchip" data-g="' + g + '" data-v="' + esc(v) + '" aria-pressed="' +
    (F[g] === v) + '">' + esc(l) + '</button>').join('');
}

function renderJournal() {
  const all = S.journal || [];
  const jobs = all.filter((j) =>
    (F.status === 'all' || (F.status === 'ok') === !!j.ok) &&
    (F.printer === 'all' || j.printer === F.printer));
  $('#jnote').textContent = jobs.length + ' van ' + all.length +
    ' bewaard (max ' + S.journal_max + ')';
  $('#jbody').innerHTML = jobs.slice(0, 40).map((j) =>
    '<tr><td class="t">' + esc(String(j.time || '').slice(5)) + '</td>' +
    '<td class="mono">' + esc(j.printer || '?') + '</td>' +
    '<td class="mono">' + esc(String(j.format || '?').toUpperCase()) +
      (Number(j.copies) > 1 ? ' ×' + j.copies : '') + '</td>' +
    '<td><span class="chip ' + (j.ok ? 'good' : 'bad') + '">' +
      icon(j.ok ? P.check : P.close, 'ico-sm') + (j.ok ? 'ok' : 'fout') + '</span></td>' +
    '<td>' + esc(j.summary || '') +
      (j.detail ? '<span class="detail">' + esc(j.detail) + '</span>' : '') + '</td></tr>').join('')
    || '<tr><td colspan="5" class="empty">Geen printjobs die hierbij passen.</td></tr>';
}

/* ---- log --------------------------------------------------------------- */
async function loadLog() {
  if (LOGBUSY) return;
  LOGBUSY = true;
  $('#logbtn').disabled = true;
  try {
    const r = await fetch('api/log?lines=250');
    const d = await r.json();
    const el = $('#log');
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
    el.textContent = d.ok ? (d.text || '(leeg)')
      : (d.error || 'Log niet beschikbaar.');
    if (stick) el.scrollTop = el.scrollHeight;
  } catch (e) {
    $('#log').textContent = 'Log niet op te halen: ' + e;
  }
  LOGBUSY = false;
  $('#logbtn').disabled = false;
}

/* ---- actions ----------------------------------------------------------- */
let toastT = null;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('on');
  clearTimeout(toastT);
  toastT = setTimeout(() => el.classList.remove('on'), 3800);
}

async function post(url, body) {
  const r = await fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  return {status:r.status, data: await r.json().catch(() => ({}))};
}

document.addEventListener('click', async (ev) => {
  const test = ev.target.closest('[data-test]');
  if (test) {
    test.disabled = true;
    toast('Testlabel naar ' + test.dataset.test + ' gestuurd…');
    const r = await post('selftest?printer=' + encodeURIComponent(test.dataset.test));
    toast(r.data.ok ? 'Testlabel geprint op ' + test.dataset.test
      : 'Testprint mislukt: ' + (r.data.summary || r.data.error || r.status));
    test.disabled = false;
    load();
    return;
  }
  const roll = ev.target.closest('[data-roll]');
  if (roll) {
    const p = roll.dataset.p;
    if (roll.dataset.roll === 'reset') {
      const r = await post('api/roll', {printer:p, action:'reset'});
      toast(r.data.ok ? 'Nieuwe rol geteld voor ' + p + ' — ±' +
        r.data.roll.capacity + ' labels' : 'Mislukt: ' + (r.data.error || r.status));
    } else {
      const cur = roll.dataset.cap;
      const v = prompt('Hoeveel labels zitten er op een volle rol voor ' + p + '?', cur);
      if (!v) return;
      const r = await post('api/roll', {printer:p, action:'capacity', capacity:parseInt(v, 10)});
      toast(r.data.ok ? 'Rolgrootte voor ' + p + ' staat op ' + r.data.roll.capacity
        : 'Mislukt: ' + (r.data.error || r.status));
    }
    load();
    return;
  }
  const chip = ev.target.closest('.fchip');
  if (chip) {
    F[chip.dataset.g] = chip.dataset.v;
    renderFilters();
    renderJournal();
  }
});

$('#logbtn').addEventListener('click', loadLog);
// Bound once on the container, not per render — renderChart runs every poll.
$('#chart').addEventListener('mouseleave', () => { $('#tip').hidden = true; });

/* ---- boot -------------------------------------------------------------- */
async function load() {
  try {
    const r = await fetch('api/state');
    S = await r.json();
    $('#live').classList.remove('off');
    $('#live-t').textContent = 'live · ' + S.now;
    render();
  } catch (e) {
    $('#live').classList.add('off');
    $('#live-t').textContent = 'geen verbinding';
  }
}

$('#brand-ico').innerHTML = icon(P.printer, 'ico-lg');
$('#i-chart').innerHTML = icon(P.chart);
$('#i-hist').innerHTML = icon(P.hist);
$('#i-log').innerHTML = icon(P.log);
$('#i-api').innerHTML = icon(P.api);
$('#apihost').textContent = location.port === '8000'
  ? location.host : 'poort 8000 op je HA-host';

load();
loadLog();
setInterval(() => { if (!document.hidden) load(); }, 10000);
setInterval(() => { if (!document.hidden) loadLog(); }, 30000);
let rz = null;
addEventListener('resize', () => {
  clearTimeout(rz);
  rz = setTimeout(() => { if (S) renderChart(); }, 150);
});
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    """The dashboard. Works both on :8000 and behind Home Assistant ingress."""
    # Under ingress the browser sits at /api/hassio_ingress/<token>/ while we
    # only ever see the stripped path, so <base> is what keeps every relative
    # fetch inside the tunnel.
    prefix = request.headers.get("X-Ingress-Path", "")
    base = (prefix.rstrip("/") + "/") if prefix else "/"
    return _DASH.replace("__BASE__", base)


def _device_models() -> dict[str, str]:
    """The model name CUPS holds per queue, from the USB device URI."""
    import urllib.parse
    out: dict[str, str] = {}
    for line in _out(_run(["lpstat", "-v"])).splitlines():
        if not line.startswith("device for "):
            continue
        queue, _, uri = line[len("device for "):].partition(": ")
        if "://" not in uri:
            continue
        rest = uri.split("://", 1)[1].split("?", 1)[0]
        maker, _, model = rest.partition("/")
        out[queue.strip()] = urllib.parse.unquote(model or maker).strip()
    return out


def _pretty_model(kind: str, device: str) -> str:
    """"ZTC ZD220-203dpi ZPL" -> "Zebra ZD220"; the name on the box."""
    name = re.sub(r"^ZTC\s+", "", device or "")
    name = re.sub(r"[-\s]\d+\s*dpi", "", name, flags=re.I)
    name = re.sub(r"\s+(ZPL|EPL|CUPS|Series).*$", "", name, flags=re.I).strip()
    maker = {"dymo": "DYMO", "zebra": "Zebra"}.get(kind, "")
    if not name:
        return maker or kind.title()
    if maker and not name.lower().startswith(maker.lower()):
        name = f"{maker} {name}"
    return name


@app.route("/api/state", methods=["GET"])
def api_state():
    """Everything the dashboard paints, in one round trip."""
    st = _status()
    devices = _device_models()
    for p in st["printers"]:
        p["roll"] = _roll_state(p["name"])
        p["device"] = devices.get(p["name"], "")
        p["title"] = _pretty_model(p.get("kind", ""), p["device"])
    return jsonify({
        "server_version": SERVER_VERSION,
        "printers": st["printers"],
        "default": st["printer"],
        "attention": st["attention"],
        "needs_attention": st["needs_attention"],
        "queue": st["queue"],
        "today": _stats_today(),
        "series": _stats_series(14),
        "journal": list(reversed(_JOURNAL))[:JOURNAL_MAX],
        "journal_max": JOURNAL_MAX,
        "now": datetime.now().strftime("%H:%M:%S"),
    })


@app.route("/api/roll", methods=["POST"])
def api_roll():
    """Reset the roll estimate ("nieuwe rol") or correct its capacity."""
    body = request.get_json(silent=True) or {}
    printer = body.get("printer") or _default_queue()
    if not _queue_exists(printer):
        return jsonify({"ok": False, "error": f"onbekende printer {printer}"}), 404
    try:
        cap = int(body["capacity"]) if body.get("capacity") is not None else None
    except (TypeError, ValueError):
        cap = None
    action = body.get("action", "reset")
    if action == "reset":
        _roll_reset(printer, cap)
    elif action == "capacity":
        if not cap:
            return jsonify({"ok": False, "error": "capacity ontbreekt"}), 400
        entry = _ROLL.get(printer) or _roll_reset(printer, cap)
        entry["capacity"] = max(1, cap)
        _roll_save()
    else:
        return jsonify({"ok": False, "error": f"onbekende actie {action}"}), 400
    return jsonify({"ok": True, "roll": _roll_state(printer)})


@app.route("/api/log", methods=["GET"])
def api_log():
    try:
        lines = int(request.args.get("lines", 200))
    except ValueError:
        lines = 200
    return jsonify(_addon_log(max(20, min(lines, 1000))))


@app.route("/old", methods=["GET"])
def index_plain():
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
def _normalize_dymo_ppds(entries: list[dict]) -> None:
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
    for entry in entries:
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


def _reset_zebra_geometry(entries: list[dict]) -> None:
    """Persist Label Shift 0 / Label Home 0,0 on every new Zebra.

    rastertolabel never sends ^LS, so a Left Position stored in the printer
    (by e.g. Zebra Setup Utilities or a Windows driver) silently shifts every
    CUPS job sideways and clips one edge. This pins the geometry defaults;
    the config-only format prints nothing and feeds no label. Only ever
    called with entries new since the last reload — a Zebra already running
    must not get this replayed at it on every timer tick.
    """
    for entry in entries:
        if entry.get("kind") == "zebra" and _queue_exists(entry["name"]):
            res = _run(["lpr", "-P", entry["name"], "-l"],
                       data=b"^XA^LS0^LH0,0^JUS^XZ\n")
            print(f"[geometry] {entry['name']}: ^LS0^LH0,0 saved "
                  f"({'ok' if res.returncode == 0 else _err(res)})",
                  flush=True)


def _warm_native_px(entries: list[dict]) -> None:
    """Measure the loaded media of every new queue up front, so the first
    /printers call after it registers answers instantly with exact geometry."""
    for entry in entries:
        name = entry.get("name", "")
        media = _default_media_for(name)
        if not name or not media or media == "auto":
            continue
        dpi = DPI_BY_KIND.get(entry.get("kind"), 300)
        px = _probe_native_px(name, media, dpi)
        print(f"[geometry] {name}: {media} -> native_px {px}", flush=True)


if __name__ == "__main__":
    _journal_load()
    _stats_load()
    _roll_load()
    _reload_configured()  # first read: every entry is "new", so all get fixed up
    threading.Thread(target=_reload_configured_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
