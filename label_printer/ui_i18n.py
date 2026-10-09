"""Dashboard catalogs and language-neutral messages stored in print history."""
import html
import json
import re
from pathlib import Path
from flask import has_request_context, request

ROOT = Path(__file__).parent / 'ui_locales'
CATALOGS = {lang: json.loads((ROOT / f'{lang}.json').read_text()) for lang in ('en', 'nl')}


def language():
    """Explicit selection, browser preference, then English fallback."""
    if not has_request_context():
        return 'en'
    chosen = request.args.get('lang')
    if chosen in CATALOGS:
        return chosen
    if chosen is not None:
        return 'en'
    return request.accept_languages.best_match(['en', 'nl']) or 'en'


def descriptor(value):
    if isinstance(value, Message):
        return value.parts
    return [{'text': str(value)}]


def render_parts(parts, lang):
    result = []
    for part in parts:
        if 'text' in part:
            result.append(part['text'])
            continue
        key = part['key']
        text = CATALOGS.get(lang, CATALOGS['en']).get(key, CATALOGS['en'].get(key, key))
        args = [render_parts(a['parts'], lang) if isinstance(a, dict) and 'parts' in a else a
                for a in part.get('args', [])]
        kwargs = {k: render_parts(v['parts'], lang) if isinstance(v, dict) and 'parts' in v else v
                  for k, v in part.get('kwargs', {}).items()}
        result.append(text.format(*args, **kwargs))
    return ''.join(result)


class Message(str):
    """Behaves as text while retaining its templates and parameters."""
    def __new__(cls, parts):
        value = super().__new__(cls, render_parts(parts, language()))
        value.parts = parts
        return value

    def __add__(self, other):
        return Message(self.parts + descriptor(other))

    def __radd__(self, other):
        return Message(descriptor(other) + self.parts)

    def format(self, *args, **kwargs):
        part = dict(self.parts[0])
        part['args'] = [_parameter(a) for a in args]
        part['kwargs'] = {k: _parameter(v) for k, v in kwargs.items()}
        return Message([part])


def _parameter(value):
    return {'parts': value.parts} if isinstance(value, Message) else value


def message(key, *args):
    # Named templates are formatted later, e.g. an alert containing {p}.
    if not args and re.search(r'\{[a-zA-Z]', key):
        value = str.__new__(Message, key)
        value.parts = [{'key': key}]
        return value
    return Message([{'key': key, 'args': [_parameter(a) for a in args]}])


def localize(value, lang):
    if isinstance(value, Message):
        return render_parts(value.parts, lang)
    if isinstance(value, dict):
        result = {k: localize(v, lang) for k, v in value.items()}
        if 'summary_i18n' in value:
            result['summary'] = '; '.join(render_parts(parts, lang) for parts in value['summary_i18n'])
        if 'detail_i18n' in value:
            result['detail'] = render_parts(value['detail_i18n'], lang)[:200]
        return result
    if isinstance(value, (list, tuple)):
        return [localize(v, lang) for v in value]
    return value


def render_page(page, lang):
    """Translate static HTML text; JavaScript uses the injected catalog."""
    catalog = CATALOGS[lang]
    keys = sorted((k for k in catalog if '{' not in k and k.strip()), key=len, reverse=True)
    pattern = re.compile('|'.join(re.escape(k) for k in keys))
    def translate_text(match):
        return '>' + pattern.sub(lambda m: html.escape(catalog[m[0]]), match[1]) + '<'
    # Never translate CSS, JavaScript identifiers or API examples.
    chunks = re.split(r'(<script>[\s\S]*?</script>|<style>[\s\S]*?</style>|<pre[\s\S]*?</pre>)', page)
    for i, chunk in enumerate(chunks):
        if not chunk.startswith(('<script>', '<style>', '<pre')):
            chunks[i] = re.sub(r'>([^<>]+)<', translate_text, chunk)
    page = ''.join(chunks)
    page = page.replace('lang="nl"', f'lang="{lang}"').replace("lang='nl'", f"lang='{lang}'")
    # Escape HTML delimiters to prevent a future translation terminating script.
    payload = json.dumps(catalog, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return page.replace('__CATALOG__', payload)


def join_messages(separator, values):
    result = Message([])
    for i, value in enumerate(values):
        if i:
            result += separator
        result += value
    return result
