"""Private local journal with atomic writes and an exclusive per-publication lock."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from .common import Error, canonical, digest, load_json, require


class State:
    def __init__(self, root, base):
        self.directory = Path(root) / digest(base)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.profile_path = self.directory / "profile.json"

    def save(self, path, value):
        fd, tmp = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(canonical(value) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            # Persist the rename as well as the file contents on POSIX.
            if hasattr(os, "O_DIRECTORY"):
                handle = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def path(self, operation_id):
        return self.directory / ("operation-" + digest(operation_id) + ".json")

    def read(self, operation_id):
        path = self.path(operation_id)
        require(path.exists(), "Операция не найдена.", "operation_not_found")
        return load_json(path)

    @contextmanager
    def lock(self):
        path = self.directory / "import.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Error("operation_locked", "Импорт уже запущен. Для аварийной блокировки см. README.") from None
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            path.unlink()

    def log(self, **event):
        # Callers supply only event names, UUIDs, HTTP status, attempt, and error code.
        from datetime import datetime, timezone
        fd = os.open(self.directory / "operations.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(canonical({"time": datetime.now(timezone.utc).isoformat(), **event}) + "\n")
