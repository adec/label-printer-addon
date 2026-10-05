import io
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'label_printer'))
import brother_network as brother
import server
from PIL import Image


def png(size=(1164, 1660)):
    buf = io.BytesIO()
    Image.new('RGBA', size, (0, 0, 0, 0)).save(buf, 'PNG')
    return buf.getvalue()


class BrotherTests(unittest.TestCase):
    def setUp(self):
        self.opts = {'brother_host': '127.0.0.1', 'brother_label': '102x152'}

    def test_geometry_all_labels_and_continuous_length(self):
        for label in brother.LABELS:
            self.assertGreater(brother.size(label, self.opts)[0], 0)
        self.assertEqual(brother.size('102x152', self.opts), [1164, 1660])
        self.assertEqual(brother.size('62', dict(self.opts, brother_length_mm=100)), [696, 1181])

    def test_reject_bad_input_and_wrong_roll_before_network(self):
        with patch.object(brother.socket, 'create_connection') as connect:
            cases = [(png((100, 100)), 'png', None, 'size_mismatch'),
                     (b'bad', 'png', None, 'bad_image'),
                     (b'^XA^XZ', 'zpl', None, 'unsupported_format'),
                     (png(), 'png', '62x100', 'invalid_media')]
            for data, fmt, media, error in cases:
                result = brother.print_job(data, fmt, media, 1,
                    dict(self.opts, brother_size_mismatch='reject'), lambda *_: None, [])
                self.assertEqual(result['error'], error)
            connect.assert_not_called()

    def test_tcp_real_raster_delivery_and_copies(self):
        received = bytearray()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        def receive():
            conn, _ = listener.accept()
            with conn:
                while chunk := conn.recv(65536):
                    received.extend(chunk)
            listener.close()
        worker = threading.Thread(target=receive)
        worker.start()
        result = brother.print_job(png((400, 600)), 'png', None, 2,
            dict(self.opts, brother_port=listener.getsockname()[1]), lambda *_: None, [])
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result['submitted'])
        self.assertFalse(result['printed'])
        self.assertEqual(bytes(received).count(b'\x1b\x69\x7a'), 2)  # media/quality command per page
        self.assertGreater(len(received), 500000)

    def test_crop_and_pdf_pages_convert(self):
        for mode in ('scale', 'crop', 'reject'):
            with patch.object(brother.socket, 'create_connection') as connect:
                result = brother.print_job(b'%PDF', 'pdf', 'auto', 1,
                    dict(self.opts, brother_size_mismatch=mode),
                    lambda *_: [Image.new('L', (1164, 1660), 255)] * 2, [])
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['pages'], 2)
                connect.return_value.__enter__.return_value.sendall.assert_called_once()

    def test_all_labels_generate_model_raster(self):
        for label in brother.LABELS:
            with self.subTest(label=label), patch.object(brother.socket, 'create_connection'):
                result = brother.print_job(png(tuple(brother.size(label, self.opts))),
                    'png', None, 1, dict(self.opts, brother_label=label), lambda *_: None, [])
                self.assertTrue(result['ok'], result)

    def test_connection_failure(self):
        with patch.object(brother.socket, 'create_connection', side_effect=TimeoutError('offline')):
            result = brother.print_job(png(), 'png', None, 1, self.opts, lambda *_: None, [])
            self.assertEqual(result['error'], 'network_print_failed')
            self.assertFalse(brother.connected(brother.config(self.opts)))

    def test_selftest_without_saved_host_returns_configuration_error(self):
        for options in ({}, {'brother_host': ''}, {'brother_label': '62'}):
            with self.subTest(options=options), patch.object(server, '_addon_options', return_value=options), \
                 patch.object(brother.socket, 'create_connection') as connect:
                reply = server.app.test_client().post('/selftest?printer=brother')
                self.assertEqual(reply.status_code, 503)
                self.assertEqual(reply.get_json()['error'], 'brother_not_configured')
                connect.assert_not_called()

    def test_selftest_invalid_configuration_is_json(self):
        for overrides in ({'brother_label': 'unknown'}, {'brother_port': 70000},
                          {'brother_length_mm': 1, 'brother_label': '62'}):
            with self.subTest(overrides=overrides), patch.object(server, '_addon_options', return_value=dict(self.opts, **overrides)):
                reply = server.app.test_client().post('/selftest?printer=brother')
                self.assertEqual(reply.status_code, 422)
                self.assertEqual(reply.get_json()['error'], 'invalid_configuration')

    def test_selftest_continuous_roll(self):
        with patch.object(server, '_addon_options', return_value=dict(self.opts, brother_label='62')), \
             patch.object(server, '_journal_add'), \
             patch.object(brother.socket, 'create_connection') as connect:
            reply = server.app.test_client().post('/selftest?printer=brother')
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(reply.get_json()['submitted'])
            connect.return_value.__enter__.return_value.sendall.assert_called_once()

    def test_api_discovery_default_and_print(self):
        with patch.object(server, '_addon_options', return_value=self.opts), \
             patch.object(server, '_run', return_value=type('Result', (), {'stdout': b'', 'stderr': b'', 'returncode': 0})()), \
             patch.object(server, '_roll_state', return_value={}), \
             patch.object(server, '_journal_add'), \
             patch.object(brother, 'connected', return_value=True), \
             patch.object(brother.socket, 'create_connection'):
            client = server.app.test_client()
            entry = client.get('/printers').get_json()['printers'][0]
            self.assertEqual(entry['name'], 'brother')
            self.assertTrue(entry['default'])
            self.assertEqual(entry['native_px'], [1164, 1660])
            self.assertNotIn('zpl', entry['accepts'])
            reply = client.post('/print', data={'file': (io.BytesIO(png()), 'label.png')})
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(reply.get_json()['submitted'])
            self.assertEqual(client.post('/selftest?printer=brother').status_code, 200)


if __name__ == '__main__':
    unittest.main()
