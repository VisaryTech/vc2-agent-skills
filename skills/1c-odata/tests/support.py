"""Shared test setup and in-memory HTTP responses; no live credentials."""
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
IDS = {"item": str(uuid.UUID(int=4))}


class Response:
    def __init__(self, status=200, body=b'{"value": []}', headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, _):
        return self.body
