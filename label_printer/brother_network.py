"""Brother QL-1110NWB raster printing over TCP (no CUPS queue required)."""
import io
import socket
import threading

from PIL import Image, ImageOps
from brother_ql.conversion import convert
from brother_ql.labels import LabelsManager
from brother_ql.raster import BrotherQLRaster
from brother_ql.exceptions import BrotherQLRasterError

NAME = "brother"
MODEL = "QL-1110NWB"
# Black-only rectangular DK rolls. Round and red/black rolls need other handling.
LABELS = ("12", "29", "38", "50", "54", "62", "102", "103",
          "17x54", "17x87", "29x42", "29x90", "39x90", "39x48",
          "60x86", "62x100",
          "102x51", "102x152", "103x164")
_LOCK = threading.Lock()


def config(options):
    host = str(options.get("brother_host") or "").strip()
    if not host:
        return {}
    label = str(options.get("brother_label") or "62")
    if label not in LABELS:
        raise ValueError("Unsupported brother_label")
    port = int(options.get("brother_port", 9100))
    if not 1 <= port <= 65535 or any(c in host for c in "/\r\n"):
        raise ValueError("Use a printer IP address or hostname and a valid TCP port")
    return {"name": NAME, "kind": NAME, "model": MODEL, "media": label,
            "raster": True, "host": host, "port": port}


def size(label, options):
    specs = LabelsManager()[label]
    w, h = specs.dots_printable
    if not h:
        length = float(options.get("brother_length_mm", 100))
        h = round(length * 300 / 25.4)
        if not 301 <= h <= 35434:
            raise ValueError("Continuous label length must be 26–3000 mm")
    return [w, h]


def media_entry(label, options):
    w, h = size(label, options)
    return {"media": label, "label": f"Brother DK {label} mm",
            "native_px": [w, h],
            "printable": {"margin_mm": dict.fromkeys(
                ("left", "right", "leading", "trailing"), 0.0),
                "rect_px": {"x": 0, "y": 0, "w": w, "h": h}}}


def connected(cfg):
    try:
        with socket.create_connection((cfg["host"], cfg["port"]), timeout=1):
            return True
    except OSError:
        return False


def print_job(data, fmt, media, copies, options, rasterize_pdf, notes):
    cfg = config(options)
    if not cfg:
        return {"ok": False, "error": "printer_not_connected", "printer": NAME}
    label = cfg["media"] if not media or media.lower() == "auto" else media
    if label != cfg["media"]:
        return {"ok": False, "error": "invalid_media", "printer": NAME,
                "hint": "Set brother_label to match the physically loaded roll."}
    if fmt not in ("png", "jpg", "pdf"):
        return {"ok": False, "error": "unsupported_format", "printer": NAME}
    try:
        target = tuple(size(label, options))
        if fmt == "pdf":
            images = rasterize_pdf(data, 300)
            if not images:
                return {"ok": False, "error": "pdf_raster_failed", "printer": NAME}
        else:
            image = Image.open(io.BytesIO(data))
            image.load()
            images = [ImageOps.exif_transpose(image)]
        mode = options.get("brother_size_mismatch", "scale")
        align = options.get("brother_crop_align", "center")
        prepared = []
        for image in images:
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", image.size, "white")
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            canvas = canvas.convert("L")
            if mode == "reject" and canvas.size != target:
                return {"ok": False, "error": "size_mismatch", "printer": NAME,
                        "expected_px": list(target), "actual_px": list(canvas.size)}
            if mode == "crop":
                x = max(0, (canvas.width - target[0]) // 2)
                y = 0 if align == "leading-edge" else max(0, (canvas.height - target[1]) // 2)
                canvas = canvas.crop((x, y, min(canvas.width, x + target[0]),
                                      min(canvas.height, y + target[1])))
            elif mode != "reject":
                canvas = ImageOps.contain(canvas, target, Image.Resampling.LANCZOS)
            fitted = Image.new("L", target, 255)
            fitted.paste(canvas, ((target[0] - canvas.width) // 2,
                                 0 if align == "leading-edge" else (target[1] - canvas.height) // 2))
            prepared.append(fitted)
        # Convert before opening a connection, so bad input cannot partially print.
        raster = BrotherQLRaster(MODEL)
        raster.exception_on_warning = True
        convert(raster, prepared * copies, label, rotate=0,
                cut=bool(options.get("brother_cut", True)), compress=False)
    except (ValueError, OSError, NotImplementedError, BrotherQLRasterError) as exc:
        return {"ok": False, "error": "bad_image", "printer": NAME, "detail": str(exc)}
    try:
        # One stream at a time: concurrent requests must not interleave jobs.
        with _LOCK, socket.create_connection((cfg["host"], cfg["port"]), timeout=10) as sock:
            sock.sendall(raster.data)
    except OSError as exc:
        return {"ok": False, "error": "network_print_failed", "printer": NAME,
                "detail": str(exc), "hint": "Check power, address and TCP port. A partial send may have printed labels; check before retrying."}
    notes.append(f"Brother raster 300 dpi, {target[0]}×{target[1]} px, {mode}; sent over TCP")
    # TCP delivery is not proof that a physical label came out.
    return {"ok": True, "printed": False, "submitted": True, "printer": NAME,
            "media": label, "copies": copies, "pages": len(prepared),
            "format": fmt, "size_policy": mode,
            "hint": "Sent to printer; physical completion is not confirmed."}
