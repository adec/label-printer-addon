import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'label_printer'))
import server
from grocy_labels import GrocyEnricher, GrocyLookupError, label_data, render_label
from PIL import Image
import zxingcpp


PAYLOAD = {
    'product': 'Chicken Curry', 'grocycode': 'grcy:p:42:stock123',
    'due_date': 'DD: 2026-12-12',
    'details': {'location': {'id': '2', 'name': 'Freezer'},
                'quantity_unit_stock': {'name': 'portion', 'name_plural': 'portions'}},
    'stock_entry': {'amount': '4', 'purchased_date': '2026-09-05', 'location_id': '2'},
    'contents': 'Chicken Curry', 'category': 'Dishes',
}


def geometry(size=(696, 1181), media='62'):
    return {'name': 'brother', 'connected': True,
            'loaded': {'media': media, 'native_px': list(size)}, 'detection': None}


class GrocyLabelTests(unittest.TestCase):
    def setUp(self):
        options = patch.object(server, '_addon_options', return_value={
            'brother_host': '127.0.0.1', 'brother_label': '62',
            'brother_length_mm': 100, 'brother_size_mismatch': 'reject'})
        options.start()
        self.addCleanup(options.stop)

    def test_native_payload_preserves_stock_specific_scan_code(self):
        data = label_data(PAYLOAD)
        self.assertEqual(data.code, PAYLOAD['grocycode'])
        self.assertEqual(data.stored, '5 Sep 2026')
        self.assertEqual(data.due, '12 Dec 2026')
        self.assertEqual(data.quantity, '4 portions')
        self.assertEqual(data.location, 'Freezer')

    def test_product_date_type_overrides_static_heading(self):
        for due_type, heading in ((1, 'BEST BEFORE'), ('2', 'EXPIRES')):
            payload = dict(PAYLOAD, date_label='EAT BEFORE', details={
                'product': {'id': '42', 'due_type': due_type},
                'product_group': {'name': 'Prepared meals'}})
            data = label_data(payload)
            self.assertEqual(data.date_label, heading)
            self.assertEqual(data.category, 'Prepared meals')
        self.assertEqual(label_data(PAYLOAD).date_label, 'DUE DATE')

    def test_api_fills_product_group_and_date_type_without_replacing_stock_date(self):
        calls = []
        def get(path):
            calls.append(path)
            if path == '/api/stock/products/42':
                return {'product': {'id': '42', 'due_type': '2', 'product_group_id': '7'},
                        'next_due_date': '2026-10-01', 'stock_amount': 99,
                        'location': {'id': '2', 'name': 'Freezer'}}
            return {'id': '7', 'name': 'Prepared meals'}
        rich = GrocyEnricher(get).enrich(PAYLOAD)
        data = label_data(rich)
        self.assertEqual(data.date_label, 'EXPIRES')
        self.assertEqual(data.category, 'Prepared meals')
        self.assertEqual(data.due, '12 Dec 2026')
        self.assertEqual(data.quantity, '4 portions')
        self.assertEqual(data.code, PAYLOAD['grocycode'])
        self.assertEqual(calls, ['/api/stock/products/42', '/api/objects/product_groups/7'])

    def test_webhook_product_details_avoid_redundant_product_lookup(self):
        calls = []
        def get(path):
            calls.append(path)
            return {'id': '7', 'name': 'Prepared meals'}
        payload = dict(PAYLOAD, details=dict(PAYLOAD['details'], product={
            'id': '42', 'due_type': '1', 'product_group_id': '7'}))
        data = label_data(GrocyEnricher(get).enrich(payload))
        self.assertEqual(data.category, 'Prepared meals')
        self.assertEqual(calls, ['/api/objects/product_groups/7'])

    def test_unknown_or_ungrouped_product_never_gets_static_category(self):
        payload = dict(PAYLOAD, details={'product': {
            'id': '42', 'due_type': '1', 'product_group_id': None}})
        self.assertEqual(label_data(GrocyEnricher().enrich(payload)).category, '')

    def test_identity_mismatch_and_api_failure_prevent_printing(self):
        with self.assertRaises(ValueError):
            GrocyEnricher().enrich(dict(PAYLOAD, details={'product': {'id': '999'}}))
        with patch.object(server, '_grocy_enrich', side_effect=GrocyLookupError('lookup failed')), \
             patch.object(server, '_print_bytes') as send:
            response = server.app.test_client().post('/grocy/print', json=PAYLOAD)
            self.assertEqual(response.status_code, 502)
            send.assert_not_called()

    def test_real_http_enrichment_sends_api_header_and_supports_base_subpath(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get('GROCY-API-KEY')))
                if self.path.endswith('/stock/products/42'):
                    value = {'product': {'id': '42', 'due_type': 2, 'product_group_id': 7}}
                else:
                    value = {'id': 7, 'name': 'Prepared meals'}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())
            def log_message(self, *args):
                pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        try:
            reader = GrocyEnricher.from_options({
                'grocy_url': f'http://127.0.0.1:{http.server_port}/grocy',
                'grocy_api_key': 'test-key'})
            data = label_data(reader.enrich(PAYLOAD))
            self.assertEqual((data.category, data.date_label), ('Prepared meals', 'EXPIRES'))
            self.assertEqual(calls, [('/grocy/api/stock/products/42', 'test-key'),
                                     ('/grocy/api/objects/product_groups/7', 'test-key')])
        finally:
            http.shutdown()
            http.server_close()
            worker.join(2)

    def test_stock_location_is_resolved_without_using_the_product_default(self):
        def get(path):
            self.assertEqual(path, '/api/objects/locations/3')
            return {'id': '3', 'name': 'Fridge'}
        payload = dict(PAYLOAD, stock_entry={'location_id': '3'},
                       details=dict(PAYLOAD['details'], product={
                           'id': '42', 'due_type': 1, 'product_group_id': None}))
        self.assertEqual(label_data(GrocyEnricher(get).enrich(payload)).location, 'Fridge')

    def test_default_product_location_is_not_used_for_other_stock_location(self):
        data = label_data(dict(PAYLOAD, stock_entry={'location_id': '3'}))
        self.assertEqual(data.location, '')

    def test_missing_dates_and_no_expiry_sentinel_are_not_invented(self):
        data = label_data({'product': 'Rice', 'grocycode': 'grcy:p:1'})
        self.assertEqual((data.stored, data.due, data.quantity), ('', '', ''))
        data = label_data({'product': 'Rice', 'grocycode': 'grcy:p:1',
                           'due_date': 'DD: 2999-12-31'})
        self.assertEqual(data.due, 'No expiry date')

    def test_display_alias_never_changes_scannable_stock_code(self):
        data = label_data(dict(PAYLOAD, display_code='CURRY-1'))
        self.assertEqual(data.display_code, 'CURRY-1')
        self.assertEqual(zxingcpp.read_barcodes(render_label(data, (696, 1181)))[0].text,
                         PAYLOAD['grocycode'])

    def test_supported_rolls_render_at_native_size_and_decode_exact_payload(self):
        for size in ((696, 1181), (696, 1109), (306, 991), (991, 306),
                     (696, 271), (1164, 1660)):
            with self.subTest(size=size):
                image = render_label(label_data(PAYLOAD), size)
                self.assertEqual(image.size, size)
                decoded = zxingcpp.read_barcodes(image)
                self.assertEqual([x.text for x in decoded], [PAYLOAD['grocycode']])

    def test_small_media_fails_instead_of_printing_unreadable_label(self):
        with self.assertRaises(ValueError):
            render_label(label_data(PAYLOAD), (106, 301))

    def test_long_name_and_code_do_not_prevent_scanning(self):
        data = label_data(dict(PAYLOAD, product='Very long homemade chicken curry ' * 20,
                               grocycode='grcy:p:999999:abcdef1234567890abcdef1234567890'))
        image = render_label(data, (696, 1181))
        self.assertEqual(zxingcpp.read_barcodes(image)[0].text, data.code)

    def test_preview_never_prints(self):
        with patch.object(server, '_printer_entry', return_value=geometry()), \
             patch.object(server, '_print_bytes') as send:
            response = server.app.test_client().post('/grocy/image', json=PAYLOAD)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Image.open(io.BytesIO(response.data)).size, (696, 1181))
            send.assert_not_called()

    def test_form_print_uses_actual_media_and_surfaces_submission_result(self):
        with patch.object(server, '_printer_entry', return_value=geometry(media='62x100')), \
             patch.object(server, '_print_bytes', return_value={
                 'ok': True, 'submitted': True, 'printed': False}) as send:
            response = server.app.test_client().post('/grocy/print', data={
                'product': 'Chicken Curry', 'grocycode': PAYLOAD['grocycode'],
                'due_date': 'DD: 2026-12-12'})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json['printed'])
            self.assertEqual(send.call_args.args[1:5], ('62x100', 1, 'brother', 'png'))
            self.assertEqual(send.call_args.kwargs['source'], 'grocy')

    def test_not_ready_and_bad_input_do_not_print(self):
        with patch.object(server, '_printer_entry', return_value=geometry()), \
             patch.object(server, '_print_bytes') as send:
            for payload in ({}, dict(PAYLOAD, copies=0), dict(PAYLOAD, copies='bad')):
                self.assertEqual(server.app.test_client().post(
                    '/grocy/print', json=payload).status_code, 422)
            send.assert_not_called()
        for entry in (dict(geometry(), connected=False),
                      dict(geometry(), detection={'no_media': True}),
                      dict(geometry(), detection={'error_bytes': [0, 16]})):
            with patch.object(server, '_printer_entry', return_value=entry), \
                 patch.object(server, '_print_bytes') as send:
                self.assertEqual(server.app.test_client().post(
                    '/grocy/print', json=PAYLOAD).status_code, 503)
                send.assert_not_called()

    def test_media_change_error_surfaces_without_retry(self):
        with patch.object(server, '_printer_entry', return_value=geometry()), \
             patch.object(server, '_print_bytes', return_value={
                 'ok': False, 'error': 'invalid_media'}) as send:
            response = server.app.test_client().post('/grocy/print', json=PAYLOAD)
            self.assertEqual(response.status_code, 422)
            send.assert_called_once()

    def test_unconfigured_printer_is_actionable_json(self):
        with patch.object(server, '_addon_options', return_value={}), \
             patch.object(server, '_queues', return_value=[]):
            response = server.app.test_client().post('/grocy/print', json=PAYLOAD)
            self.assertEqual(response.status_code, 503)
            self.assertIn('/printers', response.json['detail'])

    def test_cups_printers_receive_native_png_and_selected_queue(self):
        for name, dpi, size in (('dymo', 300, (696, 1109)),
                                 ('zebra', 203, (812, 1218))):
            entry = dict(geometry(size, 'Custom.62x100mm'), name=name,
                         kind=name, dpi=dpi, accepts=['png'])
            with self.subTest(name=name), \
                 patch.object(server, '_queues', return_value=['brother', name]), \
                 patch.object(server, '_printer_entry', return_value=entry) as discover, \
                 patch.object(server, '_print_bytes', return_value={'ok': True}) as send:
                response = server.app.test_client().post(
                    '/grocy/print?printer=' + name, json=PAYLOAD)
                self.assertEqual(response.status_code, 200)
                discover.assert_called_once_with(name)
                args = send.call_args.args
                self.assertEqual(args[1:], ('Custom.62x100mm', 1, name, 'png'))
                image = Image.open(io.BytesIO(args[0]))
                self.assertEqual(image.size, size)
                self.assertAlmostEqual(image.info['dpi'][0], dpi, places=1)
                self.assertEqual(zxingcpp.read_barcodes(image)[0].text, PAYLOAD['grocycode'])

    def test_default_cups_queue_without_brother_and_payload_selection(self):
        entry = dict(geometry(), name='dymo', kind='dymo', accepts=['png'])
        with patch.object(server, '_addon_options', return_value={}), \
             patch.object(server, '_queues', return_value=['dymo']), \
             patch.object(server, '_default_queue', return_value='dymo'), \
             patch.object(server, '_printer_entry', return_value=entry) as discover:
            client = server.app.test_client()
            for payload in (PAYLOAD, dict(PAYLOAD, printer='dymo')):
                self.assertEqual(client.post('/grocy/image', json=payload).status_code, 200)
            self.assertEqual(discover.call_args.args, ('dymo',))

    def test_unknown_and_unsupported_printers_never_submit(self):
        with patch.object(server, '_queues', return_value=['brother']), \
             patch.object(server, '_print_bytes') as send:
            response = server.app.test_client().post('/grocy/print?printer=missing', json=PAYLOAD)
            self.assertEqual(response.status_code, 503)
            send.assert_not_called()
        entry = dict(geometry(), name='raw', kind='unknown', accepts=[])
        with patch.object(server, '_printer_entry', return_value=entry), \
             patch.object(server, '_print_bytes') as send:
            self.assertEqual(server.app.test_client().post('/grocy/print', json=PAYLOAD).status_code, 503)
            send.assert_not_called()

    def test_raw_zebra_bitmap_decodes_and_preview_remains_png(self):
        import re
        from PIL import ImageOps
        size = (812, 1218)
        entry = dict(geometry(size, 'Custom.102x152mm'), name='zebra',
                     kind='zebra', dpi=203, accepts=['zpl'])
        with patch.object(server, '_queues', return_value=['zebra']), \
             patch.object(server, '_printer_entry', return_value=entry), \
             patch.object(server, '_print_bytes', return_value={'ok': True}) as send:
            client = server.app.test_client()
            response = client.post('/grocy/print?printer=zebra', json=PAYLOAD)
            self.assertEqual(response.status_code, 200)
            zpl, media, copies, name, fmt = send.call_args.args
            self.assertEqual((name, fmt), ('zebra', 'zpl'))
            match = re.search(rb'\^GFA,(\d+),(\d+),(\d+),([0-9A-F]+)\^FS', zpl)
            self.assertIsNotNone(match)
            total, used, stride = map(int, match.groups()[:3])
            self.assertEqual((total, used, stride), (102 * 1218, 102 * 1218, 102))
            bitmap = Image.frombytes('1', size, bytes.fromhex(match[4].decode()))
            image = ImageOps.invert(bitmap.convert('L'))
            self.assertEqual(zxingcpp.read_barcodes(image)[0].text, PAYLOAD['grocycode'])
            send.reset_mock()
            response = client.post('/grocy/image?printer=zebra', json=PAYLOAD)
            self.assertEqual(response.mimetype, 'image/png')
            self.assertEqual(Image.open(io.BytesIO(response.data)).size, size)
            send.assert_not_called()

    def test_nested_php_form_matches_json(self):
        with patch.object(server, '_printer_entry', return_value=geometry()):
            form = {'product': PAYLOAD['product'], 'grocycode': PAYLOAD['grocycode'],
                    'due_date': PAYLOAD['due_date'], 'contents': PAYLOAD['contents'],
                    'category': PAYLOAD['category'],
                    'details[location][id]': '2', 'details[location][name]': 'Freezer',
                    'details[quantity_unit_stock][name]': 'portion',
                    'details[quantity_unit_stock][name_plural]': 'portions',
                    'stock_entry[amount]': '4', 'stock_entry[location_id]': '2',
                    'stock_entry[purchased_date]': '2026-09-05'}
            client = server.app.test_client()
            self.assertEqual(client.post('/grocy/image', json=PAYLOAD).data,
                             client.post('/grocy/image', data=form).data)

    def test_http_endpoint_generates_real_brother_raster_over_tcp(self):
        received = bytearray()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)

        def receive():
            try:
                conn, _ = listener.accept()
                with conn:
                    while chunk := conn.recv(65536):
                        received.extend(chunk)
            finally:
                listener.close()

        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        with patch.object(server, '_printer_entry', return_value=geometry()), \
             patch.object(server, '_addon_options', return_value={
                 'brother_host': '127.0.0.1', 'brother_label': '62',
                 'brother_port': listener.getsockname()[1], 'brother_length_mm': 100,
                 'brother_size_mismatch': 'reject'}), \
             patch.object(server, '_journal_add'):
            response = server.app.test_client().post('/grocy/print', json=PAYLOAD)
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['submitted'])
        self.assertFalse(response.json['printed'])
        self.assertEqual(bytes(received).count(b'\x1b\x69\x7a'), 1)
        self.assertGreater(len(received), 100000)


if __name__ == '__main__':
    unittest.main()
