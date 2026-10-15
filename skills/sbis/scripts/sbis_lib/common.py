"""Shared limits, validation errors and output redaction."""

HOSTS = {"production": "https://online.sbis.ru", "test": "https://fix-online.sbis.ru"}


MAX_REQUEST = 100 * 1024 * 1024


MAX_FILE = 60 * 1024 * 1024


DEFAULT_MAX_PAGES = 10000


def format_size(byte_count):
    return f"{byte_count / (1024 * 1024):g} МиБ"


SECRET_KEYS = {"пароль", "password", "token", "access_token", "sid", "sbis_session_id",
               "x-sbissessionid", "x-sbisaccesstoken", "app_secret", "secret_key",
               "sbis_app_secret", "sbis_secret_key"}


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, message, code="invalid_input"):
    if not condition:
        raise ClientError(code, message)


def objects(value):
    """Saby permits a singleton object in place of an array."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    require(isinstance(value, list) and all(isinstance(x, dict) for x in value),
            "Ожидался объект или массив объектов.", "invalid_response")
    return value


def redact(value, secrets=(), preview=False):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in SECRET_KEYS else
                    {"base64_characters": len(v)} if preview and k == "ДвоичныеДанные" and isinstance(v, str)
                    else redact(v, secrets, preview)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x, secrets, preview) for x in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
    return value
