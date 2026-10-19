"""Bounded HTTPS requests; never forward Bearer credentials to redirects."""
import json
import urllib.error
import urllib.parse
import urllib.request
from .common import MAX_BYTES, require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, url, payload, headers):
        if payload is None:
            data = None
        elif headers.get("Content-Type") == "application/x-www-form-urlencoded":
            data = urllib.parse.urlencode(payload).encode("ascii")
        else:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        require(data is None or len(data) <= MAX_BYTES, "Запрос превышает лимит 100 МиБ.")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # Do not read or expose arbitrary server error bodies.
            status, response_headers = exc.code, dict(exc.headers)
            exc.close()
            return status, response_headers, b""
        with response:
            body = response.read(MAX_BYTES + 1)
            require(len(body) <= MAX_BYTES, "Ответ превышает лимит 100 МиБ.", "response_too_large")
            return response.status, dict(response.headers), body
