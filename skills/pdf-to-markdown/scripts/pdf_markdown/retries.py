"""Bounded OCR attempts with persisted input, transforms and raw responses."""
from pathlib import Path
from copy import deepcopy
import math
from types import SimpleNamespace

import fitz

from .cache import digest_file, save_json
from .quality import PROFILE, assess_ocr, flag, overlap, text_flags


def attempt(engine, pix, page, directory, name, parameters):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / f'{name}.png'
    pix.save(image)
    data = {'id': name, 'parameters': parameters, 'input': str(image), 'input_sha256': digest_file(image),
            'lines': [], 'raw_response': None, 'error': None}
    engine.last_raw = None
    try:
        data['lines'] = deepcopy(engine.predict(pix, page))
        for line in data['lines']:
            line['attempt'] = name
    except Exception as exc:
        data['error'] = str(exc)
    data['raw_response'] = engine.last_raw
    # Mocks and third-party adapters can omit the raw payload; disclose this explicitly.
    data['raw_response_available'] = data['raw_response'] is not None
    save_json(directory / f'{name}.json', data)
    return data


def region_input(page, box, dpi, enhanced):
    clip = fitz.Rect(box) * page.rotation_matrix
    pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=fitz.csRGB, alpha=False)
    scale = 72 / dpi
    width, height = pix.width * scale, pix.height * scale
    offset = fitz.Matrix(1, 0, 0, 1, pix.x * scale, pix.y * scale)
    inverse = fitz.Identity
    if enhanced:
        from PIL import Image, ImageOps
        image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        image = ImageOps.autocontrast(ImageOps.grayscale(image)).convert('RGB')
        image = image.transpose(Image.Transpose.ROTATE_90)
        pix = fitz.Pixmap(fitz.csRGB, image.width, image.height, image.tobytes(), False)
        inverse = fitz.Matrix(0, 1, -1, 0, width, 0)
        width, height = height, width
    matrix = inverse * offset * page.derotation_matrix
    proxy = SimpleNamespace(rect=fitz.Rect(0, 0, width, height),
                            derotation_matrix=matrix, number=page.number)
    return pix, proxy, list(matrix)


def split_region(box):
    """Keep retry tiles below the detector's downsampling limit, including at 600 dpi."""
    rect = fitz.Rect(box)
    side_points = PROFILE['ocr_max_side'] * 72 / PROFILE['max_retry_dpi']
    columns = max(1, math.ceil(rect.width / side_points))
    rows = max(1, math.ceil(rect.height / side_points))
    # Generate at most one more than the budget; omitted areas remain explicitly flagged.
    boxes = []
    for index in range(min(columns * rows, PROFILE['max_regions'] + 1)):
        x, y = index % columns, index // columns
        boxes.append([rect.x0 + x * rect.width / columns, rect.y0 + y * rect.height / rows,
                      rect.x0 + (x+1) * rect.width / columns, rect.y0 + (y+1) * rect.height / rows])
    return boxes, columns * rows - len(boxes)


def run_ocr(page, pix, engine, directory, dpi, native, images, initial_flags):
    attempts = [attempt(engine, pix, page, directory, 'full',
                        {'dpi': dpi, 'bbox': list(page.rect * page.derotation_matrix),
                         'page_rotation': page.rotation, 'contrast': False,
                         'coordinate_space': 'unrotated PDF points'})]
    lines = attempts[0]['lines']
    flags = assess_ocr(lines, native, images)
    # Engine/import failures are execution failures, not reasons for 16 identical retries.
    if attempts[0]['error']:
        return lines, attempts, flags + [flag('ocr_error', error=attempts[0]['error'])]
    regions = []
    omitted = 0
    residual_native = [f for f in initial_flags if f['code'] not in (
        'image_text_unconfirmed', 'no_native_text')]
    for issue in flags + residual_native:
        box = issue.get('bbox') or list(page.rect * page.derotation_matrix)
        box = list(fitz.Rect(box) & (page.rect * page.derotation_matrix))
        if fitz.Rect(box).is_empty:
            continue
        tiles, extra = split_region(box)
        omitted += extra
        for tile in tiles:
            if not any((fitz.Rect(tile) & fitz.Rect(other)).get_area() /
                       max(1, (fitz.Rect(tile) | fitz.Rect(other)).get_area()) > .9 for other in regions):
                regions.append(tile)
    regions.sort(key=lambda b: fitz.Rect(b).get_area())
    if len(regions) > PROFILE['max_regions'] or omitted:
        flags.append(flag('retry_region_limit', omitted=max(0, len(regions) - PROFILE['max_regions']) + omitted))
    persistent = [f for f in flags if f['code'] == 'retry_region_limit']
    for index, box in enumerate(regions[:PROFILE['max_regions']]):
        for step in range(PROFILE['region_attempts']):
            retry_dpi = min(PROFILE['max_retry_dpi'], max(dpi, PROFILE['retry_dpi']) + step * 150)
            name = f'region-{index+1:02d}-{step+1}'
            parameters = {'dpi': retry_dpi, 'bbox': box, 'contrast': bool(step),
                          'rotation_ccw': 90 if step else 0, 'page_rotation': page.rotation}
            try:
                crop, proxy, matrix = region_input(page, box, retry_dpi, bool(step))
                parameters['local_to_pdf'] = matrix
                data = attempt(engine, crop, proxy, directory, name, parameters)
            except Exception as exc:
                data = {'id': name, 'parameters': parameters, 'error': str(exc), 'lines': []}
                save_json(Path(directory) / f'{name}.json', data)
            attempts.append(data)
            if data['error']:
                persistent.append(flag('region_ocr_error', box, error=data['error']))
                break
            candidate = data['lines']
            # Retain all outputs as evidence. Exact duplicates will merge in the layout.
            lines = lines + candidate
            good = bool(candidate) and all(l['confidence'] >= PROFILE['confidence'] and
                                          not text_flags(l['text']) for l in candidate)
            disagree = any(f['code'] == 'source_disagreement' for f in assess_ocr(candidate, native, []))
            if good and not disagree:
                break
        else:
            persistent.append(flag('retry_exhausted', box))
    return lines, attempts, assess_ocr(lines, native, images) + persistent
