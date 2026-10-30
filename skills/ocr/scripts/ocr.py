#!/usr/bin/env python3
"""PaddleOCR API client; Python standard library only."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib import error, parse, request
import uuid


class OCRError(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self):
        self.base = os.environ.get('OCR_API_URL', '').strip().rstrip('/')
        self.key = os.environ.get('OCR_API_KEY', '')
        if not self.base:
            raise OCRError('Set OCR_API_URL in the calling environment')
        if not self.key:
            raise OCRError('Set OCR_API_KEY in the calling environment')
        url = parse.urlsplit(self.base)
        if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise OCRError('OCR_API_URL must be an HTTP(S) base URL without credentials, query or fragment')

    def call(self, path, data=None, content_type=None, timeout=30):
        headers = {'X-API-Key': self.key, 'Accept': 'application/json'}
        if content_type:
            headers['Content-Type'] = content_type
        req = request.Request(self.base + path, data=data, headers=headers)
        try:
            with request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
                return json.load(response)
        except error.HTTPError as exc:
            raise OCRError(f'HTTP {exc.code}: {exc.read().decode("utf-8", "replace")[:1000]}') from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise OCRError(f'Network error: {exc}; do not automatically resubmit uploads') from exc
        except ValueError as exc:
            raise OCRError('Service returned invalid JSON') from exc

    def submit(self, path, unwarp):
        mime = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg', '.tif': 'image/tiff', '.tiff': 'image/tiff'}.get(path.suffix.lower())
        if not mime:
            raise OCRError('Supported files: PDF, PNG, JPEG, TIFF')
        boundary = uuid.uuid4().hex
        # Fixed ASCII name avoids multipart header injection from local filenames.
        prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="unwarp"\r\n\r\n'
                  f'{str(unwarp).lower()}\r\n--{boundary}\r\n'
                  f'Content-Disposition: form-data; name="file"; filename="document{path.suffix.lower()}"\r\n'
                  f'Content-Type: {mime}\r\n\r\n').encode()
        body = prefix + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
        return self.call('/v1/ocr/jobs', body, f'multipart/form-data; boundary={boundary}', timeout=120)


def job_path(job_id):
    return '/v1/ocr/jobs/' + parse.quote(job_id, safe='')


def wait_result(client, job_id, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OCRError(f'Wait timeout; job_id={job_id}; resume with result {job_id}')
        state = client.call(job_path(job_id), timeout=min(30, remaining))
        status = state.get('status')
        if status == 'succeeded':
            return client.call(job_path(job_id) + '/result')
        if status == 'failed':
            raise OCRError(f'Job {job_id} failed: {state.get("error")}')
        if status not in ('queued', 'running', 'processing'):
            raise OCRError(f'Unknown status for {job_id}: {status!r}')
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def save_result(result, directory):
    if result.get('status') != 'succeeded':
        raise OCRError('Result does not report succeeded')
    if not isinstance(result.get('text'), str) or not isinstance(result.get('markdown'), str):
        raise OCRError('Result missing text or markdown; inspect service contract')
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (directory / 'text.txt').write_text(result['text'], encoding='utf-8')
    (directory / 'document.md').write_text(result['markdown'], encoding='utf-8')
    return {'job_id': result.get('id'), 'status': 'succeeded', 'page_count': result.get('page_count'),
            'output_dir': str(directory.resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check')
    status = commands.add_parser('status')
    status.add_argument('job_id')
    for name in ('recognize', 'result'):
        command = commands.add_parser(name)
        command.add_argument('file' if name == 'recognize' else 'job_id')
        command.add_argument('--output-dir', required=True, type=Path)
        command.add_argument('--wait-seconds', type=float, default=300)
        if name == 'recognize':
            command.add_argument('--unwarp', action='store_true')
    args = parser.parse_args()
    job_id = getattr(args, 'job_id', None)
    client = None
    try:
        client = Client()
        if args.command == 'check':
            health = client.call('/health')
            ready = client.call('/ready')
            if health.get('status') != 'ok' or ready.get('status') != 'ready':
                raise OCRError(f'Service not ready: health={health}, ready={ready}')
            try:
                client.call(job_path('availability-check-' + uuid.uuid4().hex))
            except OCRError as exc:
                if not isinstance(exc.__cause__, error.HTTPError) or exc.__cause__.code != 404:
                    raise
            output = {'health': health, 'ready': ready, 'api_key': 'accepted'}
        elif args.command == 'status':
            output = client.call(job_path(job_id))
        else:
            if not 0 < args.wait_seconds < float('inf'):
                raise OCRError('--wait-seconds must be finite and positive')
            if args.output_dir.exists():
                raise OCRError('Output directory already exists; choose a new directory')
            if args.command == 'recognize':
                submission = client.submit(Path(args.file), args.unwarp)
                job_id = submission.get('id')
                if not isinstance(job_id, str) or not job_id:
                    raise OCRError('Upload response has no job id; do not resubmit automatically')
                print(json.dumps({'job_id': job_id, 'status': submission.get('status')}), file=sys.stderr, flush=True)
            output = save_result(wait_result(client, job_id, args.wait_seconds), args.output_dir)
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (OCRError, OSError) as exc:
        message = str(exc)
        if client:
            message = message.replace(client.key, '[REDACTED]')
        print(json.dumps({'error': message, 'job_id': job_id}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
