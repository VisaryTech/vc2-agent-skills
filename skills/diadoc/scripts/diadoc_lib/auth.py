"""Literal dotenv loading and in-memory Refresh Token Flow credentials."""
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
from .common import ClientError, require

ENV_KEYS = ("DIADOC_CLIENT_ID", "DIADOC_CLIENT_SECRET", "DIADOC_REFRESH_TOKEN")
TOKEN_URL = "https://identity.kontur.ru/connect/token"


def validate_credentials(credentials, code="credentials_required"):
    missing = [key for key in ENV_KEYS if not isinstance(credentials.get(key), str)
               or not credentials[key].strip()]
    require(not missing, "Не заданы обязательные переменные: " + ", ".join(missing) + ".", code)


def validate_env(env=None, env_file=None):
    """Check configured credentials without authentication or exposing values."""
    try:
        credentials = load_credentials(env, env_file)
    except ClientError as exc:
        raise ClientError("configuration_error", str(exc)) from None
    validate_credentials(credentials, code="configuration_error")
    return {"ok": True}


class TokenProvider:
    def __init__(self, credentials, transport):
        self.credentials = dict(credentials)
        self.transport = transport
        self.secrets = [value for value in credentials.values() if isinstance(value, str) and value]
        self.token = ""
        self.refresh_at = 0

    def get_token(self):
        if self.token and time.monotonic() < self.refresh_at:
            return self.token
        validate_credentials(self.credentials)
        started = time.monotonic()
        payload = {"grant_type": "refresh_token", "client_id": self.credentials[ENV_KEYS[0]],
                   "client_secret": self.credentials[ENV_KEYS[1]],
                   "refresh_token": self.credentials[ENV_KEYS[2]]}
        try:
            status, _, body = self.transport.request("POST", TOKEN_URL, payload, {
                "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        except (OSError, urllib.error.URLError, HTTPException):
            raise ClientError("network_error", "Ошибка сети или таймаут при авторизации; запрос не повторялся.") from None
        require(status == 200, "Сервер авторизации отказал в выдаче токена; проверьте параметры приложения и refresh token.",
                "authentication_failed")
        try:
            result = json.loads(body)
            require(isinstance(result, dict), "Некорректный ответ авторизации.", "invalid_response")
            for key in ("access_token", "refresh_token", "id_token"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    self.secrets.append(value)
            token, lifetime = result.get("access_token"), result.get("expires_in")
            require(isinstance(token, str) and token and not any(c.isspace() for c in token)
                    and isinstance(result.get("token_type"), str) and result["token_type"].lower() == "bearer"
                    and type(lifetime) in (int, float) and math.isfinite(lifetime) and lifetime > 0,
                    "Некорректный ответ авторизации.", "invalid_response")
            if "refresh_token" in result:
                refresh = result["refresh_token"]
                require(isinstance(refresh, str) and refresh.strip(),
                        "Некорректный ответ авторизации.", "invalid_response")
                self.credentials["DIADOC_REFRESH_TOKEN"] = refresh
            self.token = token
            self.refresh_at = started + max(0, lifetime - 60)
            return token
        except (ValueError, UnicodeError, OverflowError):
            raise ClientError("invalid_response", "Некорректный ответ авторизации.") from None


def load_credentials(env=None, env_file=None):
    """Merge process credentials with literal dotenv values, without changing os.environ."""
    source = os.environ if env is None else env
    values = {}
    if env_file is not None or any(key not in source for key in ENV_KEYS):
        path = Path(env_file) if env_file is not None else Path.cwd() / ".env"
        try:
            content = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            if env_file is not None:
                raise ClientError("config_error", "Не удалось прочитать указанный файл окружения.") from None
            content = ""
        except (OSError, UnicodeError):
            raise ClientError("config_error", "Не удалось прочитать файл окружения.") from None
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            require(match is not None, "Некорректная строка в файле окружения.", "config_error")
            key, value = match.groups()
            if value.startswith(("'", '"')):
                end = value.find(value[0], 1)
                require(end >= 0 and (not value[end + 1:].strip()
                        or value[end + 1:].strip().startswith("#")),
                        "Некорректные кавычки в файле окружения.", "config_error")
                value = value[1:end]
            else:
                value = "" if value.startswith("#") else re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if key in ENV_KEYS:
                values[key] = value
    values.update({key: source[key] for key in ENV_KEYS if key in source})
    return values
