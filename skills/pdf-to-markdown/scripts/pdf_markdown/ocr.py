"""Lazy PaddleOCR adapter and its configuration."""
from dataclasses import dataclass
import json
import os
from pathlib import Path

import fitz


def automatic_models(lang):
    """Fingerprint known resolved default weights, otherwise disclose the limitation."""
    from .cache import model_signature
    names = ('PP-OCRv5_mobile_det', 'cyrillic_PP-OCRv5_mobile_rec') if lang in (
        'ru', 'ru,en', 'en,ru') else ()
    root = Path(os.environ.get('PADDLE_PDX_CACHE_HOME', Path.home() / '.paddlex')) / 'official_models'
    return {name: model_signature(root / name) if (root / name).is_dir() else None for name in names}


@dataclass(frozen=True)
class OCRConfig:
    lang: str = "ru,en"
    threads: int = 8
    det_model_dir: str | None = None
    rec_model_dir: str | None = None


class PaddleEngine:
    def __init__(self, config: OCRConfig):
        self.config = config
        self.engine = None
        self.last_raw = None

    def predict(self, pix, page):
        from .quality import PROFILE
        self.last_raw = None
        if self.engine is None:
            # Lazy import keeps native extraction and rendering lightweight.
            if self.config.det_model_dir and self.config.rec_model_dir:
                os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PaddleOCR
            options = dict(device="cpu", cpu_threads=self.config.threads,
                           enable_mkldnn=False, use_doc_orientation_classify=False,
                           use_doc_unwarping=False, use_textline_orientation=False,
                           text_det_limit_side_len=PROFILE['ocr_max_side'], text_det_limit_type="max",
                           text_rec_score_thresh=0.0)
            if self.config.lang in ("ru", "ru,en", "en,ru"):
                options.update(text_detection_model_name="PP-OCRv5_mobile_det",
                               text_recognition_model_name="cyrillic_PP-OCRv5_mobile_rec")
            else:
                options["lang"] = self.config.lang
            if self.config.det_model_dir:
                options["text_detection_model_dir"] = str(Path(self.config.det_model_dir).expanduser().resolve())
            if self.config.rec_model_dir:
                options["text_recognition_model_dir"] = str(Path(self.config.rec_model_dir).expanduser().resolve())
            self.engine = PaddleOCR(**options)
        import numpy as np
        rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        result = list(self.engine.predict(rgb[:, :, ::-1].copy()))[0]
        data = result.json
        if isinstance(data, str):
            data = json.loads(data)
        self.last_raw = data
        data = data.get("res", data)
        boxes = data.get("rec_polys", data.get("rec_boxes"))
        texts, scores = data["rec_texts"], data["rec_scores"]
        if boxes is None or not (len(boxes) == len(texts) == len(scores)):
            raise ValueError("OCR не вернул согласованные тексты, координаты и confidence")
        lines = []
        for text, score, box in zip(texts, scores, boxes):
            if len(box) == 4 and isinstance(box[0], (int, float, np.number)):
                rect = fitz.Rect([float(v) for v in box])
            else:
                rect = fitz.Rect(min(float(p[0]) for p in box), min(float(p[1]) for p in box),
                                 max(float(p[0]) for p in box), max(float(p[1]) for p in box))
            # Pixmap is rotated like the visible page. Convert back to native
            # coordinates so crops, OCR and text-layer boxes share one space.
            rect = fitz.Rect(rect.x0 * page.rect.width / pix.width,
                             rect.y0 * page.rect.height / pix.height,
                             rect.x1 * page.rect.width / pix.width,
                             rect.y1 * page.rect.height / pix.height) * page.derotation_matrix
            lines.append({"text": str(text), "confidence": float(score), "bbox": list(rect)})
        return lines
