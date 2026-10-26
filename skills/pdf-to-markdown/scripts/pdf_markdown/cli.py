"""Local PDF evidence extraction. draft.md needs visual review by an agent."""
import argparse
import logging
import math

import fitz

from .document import render
from .extraction import extract

LOG = logging.getLogger("pdf-to-markdown")


def bbox_arg(value):
    try:
        values = [float(v) for v in value.split(",")]
        if len(values) != 4 or not all(math.isfinite(v) for v in values):
            raise ValueError
        rect = fitz.Rect(values)
        if rect.is_empty or rect.is_infinite:
            raise ValueError
        return rect
    except ValueError:
        raise argparse.ArgumentTypeError("bbox: x0,y0,x1,y1 в пунктах PDF; x1>x0, y1>y0")


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Требуется положительное целое число")
    return number


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    command = sub.add_parser("extract", help="Извлечь доказательства и технический черновик")
    command.add_argument("input")
    command.add_argument("--out", required=True)
    command.add_argument("--pages")
    command.add_argument("--ocr", choices=("auto", "always", "never"), default="auto")
    command.add_argument("--lang", default="ru,en")
    command.add_argument("--dpi", type=positive_int, default=300)
    command.add_argument("--threads", type=positive_int, default=8)
    command.add_argument("--det-model-dir")
    command.add_argument("--rec-model-dir")
    command.add_argument("--refresh", action="store_true")
    command.set_defaults(run=extract)
    for name in ("render", "crop"):
        command = sub.add_parser(name)
        command.add_argument("input")
        command.add_argument("--page", type=positive_int, required=True)
        command.add_argument("--out", required=True)
        command.add_argument("--dpi", type=positive_int, default=300 if name == "render" else 600)
        if name == "crop":
            command.add_argument("--bbox", type=bbox_arg, required=True)
        command.set_defaults(run=render)
    return root


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (OSError, ValueError, RuntimeError) as exc:
        LOG.error("%s", exc)
        return 2
