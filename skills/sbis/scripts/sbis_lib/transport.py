"""Synchronous HTTPS transport and restricted file downloads."""

import json
import urllib.error
import urllib.parse
import urllib.request

from .common import MAX_FILE, MAX_REQUEST, ClientError, format_size, require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def file_url(url):
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    require(p.scheme == "https" and not p.username and not p.password and p.port in (None, 443)
            and any(host == d or host.endswith("." + d) for d in ("sbis.ru", "saby.ru")),
            "Ссылка на файл должна вести по HTTPS на домен СБИС/Saby.", "unsafe_download")
    return url


class HttpTransport:
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def post(self, url, payload, headers):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        require(len(data) <= MAX_REQUEST, f"Размер JSON-запроса превышает {format_size(MAX_REQUEST)}.")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                return response.status, response.read(MAX_REQUEST + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise

    def get(self, url):
        # Signed download URLs do not receive token headers. Validate every redirect.
        for _ in range(6):
            file_url(url)
            try:
                with self.opener.open(url, timeout=self.timeout) as response:
                    data = response.read(MAX_FILE + 1)
                    require(len(data) <= MAX_FILE, f"Файл превышает лимит клиента {format_size(MAX_FILE)}.")
                    return data
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                    url = urllib.parse.urljoin(url, exc.headers["Location"])
                    continue
                raise
        raise ClientError("unsafe_download", "Слишком много перенаправлений при скачивании.")
