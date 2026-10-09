import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'label_printer'))
import server
import ui_i18n as i18n


class TranslationTests(unittest.TestCase):
    def test_browser_language_explicit_override_and_fallback(self):
        client = server.app.test_client()
        for url, headers, lang, heading in [
            ('/', {}, 'en', 'Printed today'),
            ('/', {'Accept-Language': 'nl-NL,nl;q=0.9,en;q=0.8'}, 'nl', 'Vandaag geprint'),
            ('/?lang=en', {'Accept-Language': 'nl'}, 'en', 'Printed today'),
            ('/?lang=fr', {'Accept-Language': 'nl'}, 'en', 'Printed today'),
            ('/', {'Accept-Language': 'fr'}, 'en', 'Printed today'),
        ]:
            page = client.get(url, headers=headers).get_data(as_text=True)
            self.assertIn(f'<html lang="{lang}">', page)
            self.assertIn(f'<span class="tl">{heading}</span>', page)
            self.assertIn('id="language"', page)
            self.assertNotIn('__CATALOG__', page)
        page = client.get('/', headers={'X-Ingress-Path': '/api/hassio_ingress/test'}).get_data(as_text=True)
        self.assertIn('<base href="/api/hassio_ingress/test/">', page)

    def test_new_history_relocalizes_after_json_roundtrip(self):
        with server.app.test_request_context('/?lang=en'):
            note = i18n.message('maat exact {0}×{1}px → 1:1 doorgezet', 696, 1181)
            entry = {'summary': str(note), 'summary_i18n': [i18n.descriptor(note)]}
        stored = json.loads(json.dumps(entry))
        self.assertEqual(i18n.localize(stored, 'nl')['summary'], 'maat exact 696×1181px → 1:1 doorgezet')
        self.assertEqual(i18n.localize(stored, 'en')['summary'], 'exact size 696×1181px → sent 1:1')
        self.assertEqual(i18n.localize({'summary': 'legacy Dutch text'}, 'en')['summary'], 'legacy Dutch text')

    def test_alert_templates_nested_parameters_and_api(self):
        with server.app.test_request_context('/?lang=en'):
            alert = server._ALERT_MEANING[0][2].format(p='DYMO') + server._waiting_phrase({'count': 2})
            self.assertEqual(i18n.localize(alert, 'en'), 'The DYMO is out of labels. 2 labels are waiting.')
            self.assertEqual(i18n.localize(alert, 'nl'), 'Labels op in de DYMO. Er wachten 2 labels.')
            note = i18n.message('geweigerd: printer staat stil ({0}, ~HS)', i18n.message('pauze'))
            self.assertIn('(paused, ~HS)', i18n.localize(note, 'en'))
            self.assertIn('(pauze, ~HS)', i18n.localize(note, 'nl'))
        with patch.object(server, '_status', return_value={'printers': [], 'printer': '', 'attention': [{'message':alert}], 'needs_attention':True, 'queue':{}}), patch.object(server, '_device_models', return_value={}), patch.object(server, '_stats_today', return_value={}), patch.object(server, '_stats_series', return_value=[]):
            data = server.app.test_client().get('/api/state?lang=nl').get_json()
            self.assertIn('Er wachten 2 labels.', data['attention'][0]['message'])

    def test_catalog_parity_placeholders_and_script_embedding(self):
        en, nl = i18n.CATALOGS['en'], i18n.CATALOGS['nl']
        self.assertEqual(set(en), set(nl))
        for key in en:
            self.assertEqual(sorted(re.findall(r'\{[^}]+\}', en[key])), sorted(re.findall(r'\{[^}]+\}', nl[key])), key)
        with patch.dict(en, {'x': '</script><script>alert(1)</script>'}):
            page = i18n.render_page('<script>const catalog=__CATALOG__;</script>', 'en')
            self.assertEqual(page.count('</script>'), 1)

    def test_dashboard_javascript_runs_in_both_languages(self):
        # Execute the actual dashboard script, including status/roll/history
        # rendering, with a minimal browser surface and realistic state.
        harness = Path(__file__).with_name('dashboard_harness.cjs')
        for lang in ('en', 'nl'):
            page = server.app.test_client().get('/?lang='+lang).get_data(as_text=True)
            script = re.search(r'<script>([\s\S]*?)</script>', page)[1]
            result = subprocess.run(['node', str(harness), lang], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
