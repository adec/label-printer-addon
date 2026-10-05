"""Grocy webhook labels rendered at the printer's native pixel dimensions.

Original layout inspired by the user's Fridge Assistant label. The exact
incoming Grocycode is encoded; a displayed product ID is only a reading aid.
No guessed expiry dates, stock quantities or portion identifiers are generated.
"""
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from io import BytesIO
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from flask import Blueprint, Response, jsonify, request
from PIL import Image, ImageDraw, ImageFont
import qrcode


@dataclass(frozen=True)
class LabelData:
    name: str
    code: str
    display_code: str = ''
    code_heading: str = 'PRODUCT ID'
    location: str = ''
    category: str = ''
    stored: str = ''
    due: str = ''
    date_label: str = 'DUE DATE'
    contents: str = ''
    quantity: str = ''
    quantity_label: str = 'QUANTITY'


def _text(value, limit=1000):
    if value is None:
        return ''
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ValueError('Label text must be a string or number')
    value = str(value).strip()
    if len(value) > limit:
        raise ValueError(f'Label field exceeds {limit} characters')
    return value


def _object(value):
    if value is None or value == '':
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('details and stock_entry must be objects')
    return value


def _date(value):
    text = _text(value, 200)
    # Grocy sends a localized DD prefix followed by an ISO date.
    match = re.search(r'(\d{4}-\d{2}-\d{2})$', text)
    if match:
        try:
            parsed = date.fromisoformat(match[1])
        except ValueError as exc:
            raise ValueError('Invalid label date') from exc
        if parsed == date(2999, 12, 31):
            return 'No expiry date'
        months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
        return f'{parsed.day} {months[parsed.month - 1]} {parsed.year}'
    return text


class GrocyLookupError(RuntimeError):
    """Configured enrichment failed; do not silently print incomplete metadata."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Grocy API key to a redirect target.
        return None


class GrocyEnricher:
    """Resolve product metadata, without substituting aggregate stock dates."""
    def __init__(self, get_json=None):
        self.get_json = get_json

    @classmethod
    def from_options(cls, options):
        base = str(options.get('grocy_url') or '').rstrip('/')
        key = str(options.get('grocy_api_key') or '')
        if not base and not key:
            return cls()
        parsed = urlsplit(base)
        if (not base or not key or parsed.scheme not in ('http', 'https') or
                not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment):
            raise GrocyLookupError('Configure both grocy_url and grocy_api_key with a valid base URL')

        def get_json(path):
            req = Request(base + path, headers={'GROCY-API-KEY': key, 'Accept': 'application/json'})
            try:
                with build_opener(_NoRedirect()).open(req, timeout=3) as response:
                    raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise ValueError('Response exceeds 1 MiB')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError('Expected an object')
                return value
            except HTTPError as exc:
                raise GrocyLookupError(f'Grocy API returned HTTP {exc.code}; check API access') from None
            except (URLError, OSError, ValueError):
                # Do not expose API keys or full connection URLs in error responses.
                raise GrocyLookupError('Grocy API lookup failed; check the configured URL and API key') from None
        return cls(get_json)

    @staticmethod
    def _id(value):
        if value is None or value == '':
            return None
        text = _text(value, 20)
        if not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise ValueError('Invalid Grocy object identifier')
        return str(int(text))

    def enrich(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected a JSON object or form fields')
        rich = dict(payload)
        details = dict(_object(payload.get('details')))
        product = dict(_object(details.get('product')))
        stock = _object(payload.get('stock_entry'))
        code = _text(payload.get('grocycode'), 256).split(':')
        code_id = self._id(code[2]) if len(code) >= 3 and code[:2] == ['grcy', 'p'] else None
        product_id = self._id(product.get('id'))
        stock_product_id = self._id(stock.get('product_id'))
        identifiers = {x for x in (code_id, product_id, stock_product_id) if x}
        if len(identifiers) > 1:
            raise ValueError('Grocycode, product and stock entry refer to different products')
        identifier = next(iter(identifiers), None)
        if identifier and self.get_json and (
                str(product.get('due_type')) not in ('1', '2') or
                'product_group_id' not in product):
            fetched = _object(self.get_json(f'/api/stock/products/{identifier}'))
            fetched_product = _object(fetched.get('product'))
            if self._id(fetched_product.get('id')) != identifier:
                raise GrocyLookupError('Grocy API returned a different product')
            # Webhook metadata wins; only fill missing fields, never stock dates.
            product = dict(fetched_product, **product)
            if str(product.get('due_type')) not in ('1', '2'):
                product['due_type'] = fetched_product.get('due_type')
            details = dict(fetched, **details)
        details['product'] = product
        if 'product_group_id' in product:
            group_id = self._id(product.get('product_group_id'))
            # No group means no category chip, even if an old static DISHES param remains.
            details['product_group'] = {}
            if group_id and self.get_json:
                group = _object(self.get_json(f'/api/objects/product_groups/{group_id}'))
                if self._id(group.get('id')) != group_id:
                    raise GrocyLookupError('Grocy API returned a different product group')
                details['product_group'] = group
            elif group_id:
                candidate = _object(_object(payload.get('details')).get('product_group'))
                if self._id(candidate.get('id')) == group_id:
                    details['product_group'] = candidate
            rich['category'] = ''
        # Resolve the actual stock location when it differs from the default.
        location_id = self._id(stock.get('location_id'))
        location = _object(details.get('location'))
        if (location_id and self.get_json and
                self._id(location.get('id')) != location_id):
            actual = _object(self.get_json(f'/api/objects/locations/{location_id}'))
            if self._id(actual.get('id')) != location_id:
                raise GrocyLookupError('Grocy API returned a different stock location')
            details['location'] = actual
        rich['details'] = details
        return rich


def label_data(payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object or form fields')
    name = next((_text(payload.get(key)) for key in
                 ('product', 'battery', 'chore', 'recipe') if payload.get(key)), '')
    code = _text(payload.get('grocycode'), 256)
    if not name or not code:
        raise ValueError('A product/name and grocycode are required')
    details = _object(payload.get('details'))
    stock = _object(payload.get('stock_entry'))
    product = _object(details.get('product'))
    group = _object(details.get('product_group'))
    date_label = { '1': 'BEST BEFORE', '2': 'EXPIRY DATE' }.get(
        str(product.get('due_type', payload.get('due_type'))), 'DUE DATE')
    location = _object(details.get('location'))
    stock_location = stock.get('location_id')
    # details.location is the PRODUCT default, not necessarily this stock entry.
    location_matches = (stock_location is None or
                        str(stock_location) == str(location.get('id')))
    location_name = payload.get('location') or (
        location.get('name') if location_matches else '')
    quantity = _text(payload.get('quantity'), 120)
    if not quantity and stock.get('amount') is not None:
        unit = _object(details.get('quantity_unit_stock'))
        amount = _text(stock['amount'], 40)
        try:
            number = float(amount)
            singular = number == 1
            if number.is_integer():
                amount = str(int(number))
        except ValueError:
            singular = False
        unit_name = unit.get('name') if singular else unit.get('name_plural', unit.get('name'))
        quantity = f'{amount} {_text(unit_name, 60)}'.strip()
    display = _text(payload.get('display_code'), 120)
    heading = 'CODE' if display else 'PRODUCT ID'
    # Do not strip the batch suffix from the scannable payload.
    parts = code.split(':')
    if not display and len(parts) >= 3 and parts[:2] == ['grcy', 'p']:
        display = parts[2]
    if not display:
        display, heading = code, 'GROCYCODE'
    return LabelData(
        name=name, code=code, display_code=display, code_heading=heading,
        location=_text(location_name, 120),
        category=_text(group.get('name'), 80),
        stored=_date(payload.get('stored_date') or stock.get('purchased_date')),
        due=_date(payload.get('due_date') or stock.get('best_before_date')),
        date_label=date_label,
        contents=_text(payload.get('contents'), 2000),
        quantity=quantity,
        quantity_label=_text(payload.get('quantity_label') or 'QUANTITY', 60),
    )


@lru_cache(maxsize=128)
def _font(size, bold=False):
    name = 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf'
    for path in (name, f'/usr/share/fonts/truetype/dejavu/{name}',
                 f'/usr/share/fonts/ttf-dejavu/{name}'):
        try:
            return ImageFont.truetype(path, max(8, int(size)))
        except OSError:
            pass
    # Use a scalable fallback for development on systems without DejaVu.
    return ImageFont.load_default(size=max(8, int(size)))


def _wrap(draw, text, font, width):
    lines, current = [], ''
    for word in text.split():
        if current and draw.textlength(current + ' ' + word, font=font) > width:
            lines.append(current)
            current = ''
        candidate = (current + ' ' + word).strip()
        if draw.textlength(candidate, font=font) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ''
        # Long words are broken rather than clipped beyond the print area.
        for character in word:
            if current and draw.textlength(current + character, font=font) > width:
                lines.append(current)
                current = ''
            current += character
    if current:
        lines.append(current)
    return lines or ['']


def _draw_fitted(draw, text, box, *, start, bold=False, fill=0,
                 center=False, max_lines=2, truncate=False, minimum=8, vertical_center=False):
    x, y, width, height = box
    for size in range(max(8, int(start)), max(8, int(minimum)) - 1, -1):
        font = _font(size, bold)
        lines = _wrap(draw, text, font, width)
        line_height = max(1, draw.textbbox((0, 0), 'Ag', font=font)[3]) + max(2, size // 7)
        if len(lines) <= max_lines and len(lines) * line_height <= height:
            break
    else:
        if not truncate:
            raise ValueError('Label text cannot fit legibly on this roll')
        lines = lines[:max(1, min(max_lines, height // line_height))]
        last = lines[-1]
        while last and draw.textlength(last + '…', font=font) > width:
            last = last[:-1]
        lines[-1] = last + '…'
    if vertical_center:
        bounds = draw.textbbox((0, 0), lines[-1], font=font)
        used = (len(lines) - 1) * line_height + bounds[3] - bounds[1]
        y += (height - used) / 2
    for index, line in enumerate(lines):
        offset = (width - draw.textlength(line, font=font)) / 2 if center else 0
        top = draw.textbbox((0, 0), line, font=font)[1]
        draw.text((x + offset, y + index * line_height - top), line, font=font, fill=fill)
    return (len(lines) - 1) * line_height + draw.textbbox((0, 0), lines[-1], font=font)[3] - top


def render_label(data, size):
    if len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise ValueError('No usable native pixel dimensions available')
    native_w, native_h = size
    landscape = native_w > native_h
    width, height = (native_h, native_w) if landscape else (native_w, native_h)
    if width < 240 or height < 400 or width > 2000 or height > 36000:
        raise ValueError('Food labels need at least 240 × 400 printable pixels')
    # Extremely long configured continuous labels need not enlarge all text.
    layout_h = min(height, round(width * 2.2))
    image = Image.new('L', (width, height), 255)
    draw = ImageDraw.Draw(image)
    margin = max(10, round(width * .045))
    inner = width - 2 * margin
    scale = width / 696
    y = 0

    def area(fraction):
        return max(1, round(layout_h * fraction))

    def rule():
        draw.line((margin, y, width - margin, y), fill=0, width=max(1, round(scale * 2)))

    # Banner and optional category chip.
    banner_h = area(.085)
    draw.rounded_rectangle((0, 0, width - 1, banner_h), radius=max(6, round(16 * scale)), fill=0)
    label = (data.location or 'GROCY').upper()
    _draw_fitted(draw, label, (margin, 0, inner, banner_h),
                 start=46 * scale, bold=True, fill=255, max_lines=1,
                 truncate=True, vertical_center=True, center=True)
    y = banner_h + area(.02)
    title_h = area(.115)
    title_used = _draw_fitted(draw, data.name, (margin, y, inner, title_h),
                 start=74 * scale, bold=True, center=True, truncate=True,
                 minimum=32 * scale)
    # Advance by the actual text height, avoiding a blank second line for short names.
    y += title_used + area(.012)
    if data.category:
        group_used = _draw_fitted(draw, data.category.upper(),
                     (margin, y, inner, area(.04)), start=30 * scale,
                     bold=True, center=True, max_lines=1, truncate=True)
        y += group_used + area(.012)
    rule()
    y += area(.01)

    # Integer QR modules and four-module quiet zones; never stretch a QR image.
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=1, border=4)
    qr.add_data(data.code)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color='black', back_color='white').convert('L')
    qr_area_h = area(.285)
    gap = max(8, round(width * .025))
    qr_width = max(round(inner * .56), qr_image.width * 3)
    qr_scale = min(qr_width, qr_area_h) // qr_image.width
    if qr_scale < 3 or inner - qr_width - gap < inner * .3:
        raise ValueError('Grocycode is too dense for this roll; use a larger label')
    qr_image = qr_image.resize((qr_image.width * qr_scale, qr_image.height * qr_scale),
                               Image.Resampling.NEAREST)
    text_width = inner - qr_width - gap
    heading_h = area(.025)
    code_h = area(.075)
    text_y = y + (qr_area_h - heading_h - area(.01) - code_h) / 2
    _draw_fitted(draw, data.code_heading, (margin, text_y, text_width, heading_h),
                 start=20 * scale, bold=True, center=True, max_lines=1)
    _draw_fitted(draw, data.display_code,
                 (margin, text_y + heading_h + area(.01), text_width, code_h),
                 start=84 * scale, bold=True, center=True, max_lines=2)
    image.paste(qr_image, (width - margin - qr_width + (qr_width - qr_image.width) // 2,
                           y + (qr_area_h - qr_image.height) // 2))
    y += qr_area_h + area(.005)
    rule()
    y += area(.012)

    if data.stored:
        _draw_fitted(draw, 'STORED', (margin, y, inner, area(.021)),
                     start=21 * scale, bold=True, max_lines=1)
        y += area(.027)
        _draw_fitted(draw, data.stored, (margin, y, inner, area(.039)),
                     start=38 * scale, bold=True, max_lines=1)
        y += area(.049)
    if data.due:
        panel_h = area(.12)
        draw.rounded_rectangle((0, y, width - 1, y + panel_h),
                               radius=max(6, round(16 * scale)), fill=0)
        _draw_fitted(draw, data.date_label.upper(),
                     (margin, y + panel_h * .13, inner, panel_h * .2),
                     start=22 * scale, bold=True, fill=255, max_lines=1)
        _draw_fitted(draw, data.due, (margin, y + panel_h * .42, inner, panel_h * .45),
                     start=48 * scale, bold=True, fill=255, max_lines=2)
        y += panel_h + area(.02)
    footer_h = area(.09) if data.quantity else 0
    footer_y = layout_h - footer_h
    if data.contents and footer_y - y > area(.06):
        _draw_fitted(draw, "WHAT'S INSIDE", (margin, y, inner, area(.022)),
                     start=20 * scale, bold=True, max_lines=1)
        y += area(.032)
        _draw_fitted(draw, data.contents, (margin, y, inner, footer_y - y - area(.01)),
                     start=30 * scale, max_lines=3, truncate=True, minimum=20 * scale)
    if data.quantity:
        y = footer_y
        rule()
        y += area(.01)
        _draw_fitted(draw, data.quantity_label.upper(), (margin, y, inner, area(.02)),
                     start=20 * scale, bold=True, max_lines=1)
        y += area(.027)
        _draw_fitted(draw, data.quantity, (margin, y, inner, area(.045)),
                     start=46 * scale, bold=True, max_lines=2)
    if landscape:
        image = image.transpose(Image.Transpose.ROTATE_90)
    return image


def _request_payload():
    if request.content_length and request.content_length > 65536:
        raise ValueError('Grocy label payload exceeds 64 KiB')
    if request.is_json:
        return request.get_json(silent=True)
    source = request.args if request.method == 'GET' else request.form
    payload = {}
    # PHP/Guzzle can encode nested objects as details[location][name].
    for key, value in source.items():
        parts = re.findall(r'[^\[\]]+', key)
        if not parts or len(parts) > 5:
            raise ValueError('Invalid form field')
        target = payload
        for part in parts[:-1]:
            target = target.setdefault(part, {})
            if not isinstance(target, dict):
                raise ValueError('Conflicting form fields')
        target[parts[-1]] = value
    return payload


def register_grocy_routes(app, get_printer, send_image, enrich=None):
    """Register routes using existing printer discovery and journalled transport."""
    blueprint = Blueprint('grocy', __name__)

    def prepare():
        payload = _request_payload()
        if enrich:
            payload = enrich(payload)
        data = label_data(payload)
        copies = payload.get('copies', 1)
        if isinstance(copies, bool) or (isinstance(copies, float) and not copies.is_integer()):
            raise ValueError('copies must be an integer from 1 to 10')
        copies = int(copies)
        if not 1 <= copies <= 10:
            raise ValueError('copies must be an integer from 1 to 10')
        entry = get_printer()
        detection = entry.get('detection') or {}
        if (not entry.get('connected') or detection.get('no_media') or
                detection.get('printer_error') or any(detection.get('error_bytes', []))):
            raise RuntimeError('Brother printer is not ready; check /printers')
        loaded = entry.get('loaded') or entry
        size = loaded.get('native_px')
        media = loaded.get('media')
        if not size or not media or media == 'auto':
            raise RuntimeError('No supported label roll detected; check /printers')
        image = render_label(data, tuple(size))
        buffer = BytesIO()
        image.save(buffer, format='PNG', dpi=(300, 300))
        return buffer.getvalue(), media, copies

    @blueprint.route('/grocy/image', methods=['GET', 'POST'])
    def preview():
        try:
            png, _, _ = prepare()
            return Response(png, mimetype='image/png', headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError) as exc:
            return jsonify(ok=False, error='invalid_label', detail=str(exc)), 422
        except GrocyLookupError as exc:
            return jsonify(ok=False, error='grocy_lookup_failed', detail=str(exc)), 502
        except (RuntimeError, OSError) as exc:
            return jsonify(ok=False, error='printer_not_ready', detail=str(exc)), 503

    @blueprint.route('/grocy/print', methods=['POST'])
    def print_label():
        try:
            png, media, copies = prepare()
            # Explicit detected media prevents printing if the roll was swapped
            # between discovery and transport preflight. Never automatically retry.
            result = send_image(png, media, copies, 'brother', 'png', source='grocy')
            code = 200 if result.get('ok') else (
                422 if result.get('error') in ('invalid_media', 'size_mismatch', 'bad_image') else 503)
            return jsonify(result), code
        except (ValueError, TypeError) as exc:
            return jsonify(ok=False, error='invalid_label', detail=str(exc)), 422
        except GrocyLookupError as exc:
            return jsonify(ok=False, error='grocy_lookup_failed', detail=str(exc)), 502
        except (RuntimeError, OSError) as exc:
            return jsonify(ok=False, error='printer_not_ready', detail=str(exc)), 503

    app.register_blueprint(blueprint)
