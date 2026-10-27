"""Atomic storage, fingerprints and reusable page records."""
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import tempfile

import fitz

LOG = logging.getLogger("pdf-to-markdown")
SCHEMA = 3


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as handle:
            name = handle.name
            handle.write(text)
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def save_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def model_signature(path):
    if path is None:
        return None
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Каталог модели не найден: {root}")
    files = [(str(p.relative_to(root)), digest_file(p))
             for p in sorted(root.rglob("*")) if p.is_file()]
    if not files:
        raise ValueError(f"Каталог модели пуст: {root}")
    return {"path": str(root), "files": files}


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def settings_for(args):
    from .quality import PROFILE
    from .ocr import automatic_models
    return {"schema": SCHEMA, "ocr": args.ocr, "lang": args.lang, "dpi": args.dpi,
            "quality_profile": dict(PROFILE),
            "threads": args.threads, "pymupdf": fitz.VersionBind,
            "paddleocr": package_version("paddleocr"),
            "paddlepaddle": package_version("paddlepaddle"),
            "det_model": model_signature(args.det_model_dir),
            "rec_model": model_signature(args.rec_model_dir),
            "automatic_models": automatic_models(args.lang) if args.ocr != 'never' and not (
                args.det_model_dir and args.rec_model_dir) else {}}


def cache_key(source_hash, number, settings):
    return hashlib.sha256(json.dumps([source_hash, number, settings], sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()


def load_cached_page(cache, key, number, ocr_mode):
    """Return a reusable successful record, or None to reprocess the page."""
    if cache.exists():
        try:
            candidate = json.loads(cache.read_text(encoding="utf-8"))
            if (isinstance(candidate, dict) and candidate.get("cache_key") == key
                    and candidate.get("status") == "success"
                    and candidate.get("schema") == SCHEMA
                    and candidate.get("pdf_page") == number
                    and all(isinstance(candidate.get(field), list) for field in
                            ("native_blocks", "ocr_lines", "issues", "table_candidates",
                             "elements", "quality_flags", "attempts"))
                    and candidate.get("ocr_mode") == ocr_mode):
                immutable = cache.parent.parent / candidate['cache_path']
                if json.loads(immutable.read_text(encoding='utf-8')) == candidate:
                    return candidate
        except (ValueError, OSError, KeyError, TypeError):
            LOG.warning("Повреждённый кэш страницы %s: повторная обработка", number)
    return None
