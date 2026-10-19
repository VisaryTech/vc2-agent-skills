"""Validation and output handling."""
import uuid

MAX_BYTES = 100 * 1024 * 1024


class ClientError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def require(condition, message, code="invalid_input"):
    if not condition:
        raise ClientError(code, message)


def guid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ClientError("invalid_input", "Ожидался идентификатор UUID.") from None


def box_guid(value):
    require(isinstance(value, str), "Укажите --box-id.")
    return guid(value.removesuffix("@diadoc.ru"))


def redact(value, token="", preview=False):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in
                    {"token", "access_token", "authorization", "refresh_token", "client_secret", "client_id",
                     "id_token", "diadoc_client_id", "diadoc_client_secret", "diadoc_refresh_token"}
                    else {"base64_characters": len(v)} if preview and k == "Content" and isinstance(v, str)
                    else redact(v, token, preview)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x, token, preview) for x in value]
    if isinstance(value, str):
        secrets = [token] if isinstance(token, str) else token
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value
