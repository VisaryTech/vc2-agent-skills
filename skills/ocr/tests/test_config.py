"""Configuration contract and offline check-env behavior."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('ocr', Path(__file__).parents[1] / 'scripts/ocr.py')
ocr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocr)


class ConfigurationTests(unittest.TestCase):
    def test_check_env_is_offline_and_does_not_disclose_values(self):
        env = {'OCR_API_URL': 'https://ocr.example.org/', 'OCR_API_KEY': 'test-secret'}
        output = io.StringIO()
        with patch.dict('os.environ', env, clear=True), patch.object(ocr.sys, 'argv', ['ocr.py', 'check-env']), \
                patch.object(ocr.request, 'build_opener', side_effect=AssertionError('Network access')), \
                patch.object(ocr, 'Client', side_effect=AssertionError('Client constructed')), \
                contextlib.redirect_stdout(output):
            self.assertEqual(ocr.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {'ok': True})

    def test_missing_or_invalid_configuration_is_safe(self):
        cases = [
            {}, {'OCR_API_URL': '   ', 'OCR_API_KEY': 'test-secret'},
            {'OCR_API_URL': 'ftp://ocr.example.org', 'OCR_API_KEY': 'test-secret'},
            {'OCR_API_URL': 'https://user:test-secret@ocr.example.org', 'OCR_API_KEY': 'test-secret'},
            {'OCR_API_URL': 'http://[invalid', 'OCR_API_KEY': 'test-secret'},
            {'OCR_API_URL': 'https://ocr.example.org:99999', 'OCR_API_KEY': 'test-secret'},
            {'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': '  '},
            {'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': 'test-secret\r\n'},
            {'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': 'test-secret€'},
            {'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': 'test-secreté'},
            {'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': 'test-secret\x01'},
            {'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': 'test-secret\x7f'},
        ]
        for env in cases:
            with self.subTest(env=env):
                output = io.StringIO()
                with patch.dict('os.environ', env, clear=True), patch.object(ocr.sys, 'argv', ['ocr.py', 'check-env']), \
                        patch.object(ocr.request, 'build_opener', side_effect=AssertionError('Network access')), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(ocr.main(), 2)
                result = json.loads(output.getvalue())
                self.assertFalse(result['ok'])
                self.assertEqual(result['error'], 'configuration_error')
                self.assertNotIn('test-secret', output.getvalue())

    def test_client_uses_same_validation(self):
        with patch.dict('os.environ', {'OCR_API_URL': 'https://ocr.example.org/', 'OCR_API_KEY': 'test-secret'}, clear=True):
            client = ocr.Client()
            self.assertEqual(client.base, 'https://ocr.example.org')
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ocr.OCRError):
                ocr.Client()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {
            'OCR_API_URL': 'https://ocr.example.org', 'OCR_API_KEY': 'test-secret',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_command(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(ocr.sys, 'argv', ['ocr.py', *args]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ocr.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_check_accepts_only_expected_api_404(self):
        cases = [
            (404, b'{"detail":"Job not found or expired"}', 0),
            (404, b'<html>nginx: no route</html>', 1),
            (404, b'{"detail":"Not Found"}', 1),
            (404, b'null', 1),
            (401, b'{"detail":"Invalid API key"}', 1),
        ]
        for status, body, expected in cases:
            with self.subTest(status=status, body=body):
                responses = [
                    io.BytesIO(b'{"status":"ok"}'),
                    io.BytesIO(b'{"status":"ready"}'),
                    HTTPError('https://ocr.example.org', status, 'error', {}, io.BytesIO(body)),
                ]
                opener = Mock()
                opener.open.side_effect = responses
                with patch.object(ocr.request, 'build_opener', return_value=opener):
                    code, stdout, stderr = self.run_command('check')
                self.assertEqual(code, expected)
                if code == 0:
                    self.assertEqual(json.loads(stdout)['api_key'], 'accepted')
                else:
                    self.assertNotIn('accepted', stdout)
                    self.assertIn('error', json.loads(stderr))

    def test_check_rejects_unexpected_success_for_nonexistent_job(self):
        with patch.object(ocr.Client, 'call', side_effect=[
                {'status': 'ok'}, {'status': 'ready'}, {'status': 'succeeded'}]):
            code, stdout, stderr = self.run_command('check')
        self.assertEqual(code, 1)
        self.assertNotIn('accepted', stdout)
        self.assertIn('nonexistent job', json.loads(stderr)['error'])

    def test_request_encoding_error_is_not_reported_as_invalid_json(self):
        opener = Mock()
        opener.open.side_effect = UnicodeEncodeError('ascii', '€', 0, 1, 'unsupported')
        with patch.object(ocr.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ocr.OCRError, 'encode HTTP request'):
                ocr.Client().call('/health')

    def test_invalid_response_json_is_reported_separately(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'<html>invalid JSON</html>')
        with patch.object(ocr.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ocr.OCRError, 'Service returned invalid JSON'):
                ocr.Client().call('/health')

    def test_existing_job_commands_do_not_require_model_readiness(self):
        job_id = 'a' * 32
        state = {'id': job_id, 'status': 'succeeded'}
        result = {**state, 'text': 'Recognized text', 'markdown': '# Document', 'page_count': 1}

        def call(path, **kwargs):
            if path == ocr.job_path(job_id):
                return state
            if path == ocr.job_path(job_id) + '/result':
                return result
            raise AssertionError(f'Unexpected request: {path}')

        with patch.object(ocr.Client, 'call', side_effect=call):
            code, stdout, stderr = self.run_command('status', job_id)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout), state)
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'result'
                code, stdout, stderr = self.run_command('result', job_id, '--output-dir', str(output))
                self.assertEqual(code, 0, stderr)
                self.assertEqual((output / 'text.txt').read_text(encoding='utf-8'), result['text'])
                self.assertEqual((output / 'document.md').read_text(encoding='utf-8'), result['markdown'])
                self.assertEqual(json.loads((output / 'result.json').read_text(encoding='utf-8')), result)


if __name__ == '__main__':
    unittest.main()
