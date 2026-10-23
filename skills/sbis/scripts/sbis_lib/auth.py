"""Service credentials and private application token cache."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .common import ClientError, require


ENV_KEYS = ("SBIS_APP_CLIENT_ID", "SBIS_APP_SECRET", "SBIS_SECRET_KEY")


def validate_credentials(credentials, code="credentials_required"):
    missing = [key for key in ENV_KEYS if not isinstance(credentials.get(key), str)
               or not credentials[key].strip()]
    require(not missing, "Не заданы обязательные переменные: " + ", ".join(missing) + ".", code)


def validate_env(env=None, env_file=None):
    """Check configured credentials without authentication or token cache access."""
    try:
        credentials = load_credentials(env, env_file)
    except ClientError as exc:
        raise ClientError("configuration_error", str(exc)) from None
    validate_credentials(credentials, code="configuration_error")
    return {"ok": True}


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


class TokenCache:
    def __init__(self, profile, credentials, root=None):
        if root is not None:
            self.root = Path(root)
        else:
            xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", ""))
            cache_home = xdg_cache if xdg_cache.is_absolute() else Path.home() / ".cache"
            self.root = cache_home / "sbis"
        identity = ["service-token-v1", profile, [credentials.get(k, "") for k in ENV_KEYS]]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.path = self.root / (key + ".json")

    def load(self):
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                return None
            info = self.path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                return None
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, encoding="utf-8") as f:
                value = json.load(f).get("token")
            return value if isinstance(value, str) and value.strip() else None
        except (OSError, ValueError, AttributeError):
            return None

    def save(self, token):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not self.root.is_symlink() and self.root.stat().st_uid == os.getuid(),
                "Недоступен приватный каталог кэша.", "cache_error")
        self.root.chmod(0o700)
        fd, temp = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"token": token}, f)
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def clear(self):
        self.path.unlink(missing_ok=True)
