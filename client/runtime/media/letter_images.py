"""Pictures the user puts in a letter: bounded uploads kept in the local media folder, named by opaque IDs."""
from __future__ import annotations

import io
from pathlib import Path
import re
import uuid

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 4  # as many as one QQ turn carries
_SUFFIXES = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}
NAME = re.compile(r'letter-image-[0-9a-f]{32}\.(?:png|jpg|webp)')


def save_upload(media_root: Path, raw: bytes) -> dict:
    """Keep the bytes as sent once they decode as a PNG, JPG or WebP picture."""
    from PIL import Image
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError('LETTER_IMAGE_TOO_LARGE')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            suffix = _SUFFIXES.get(image.format)
            if suffix is None or image.width * image.height > 24_000_000:
                raise ValueError('LETTER_IMAGE_INVALID')
            width, height = image.size
            image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError('LETTER_IMAGE_INVALID') from None
    image_id = uuid.uuid4().hex
    media_root.mkdir(parents=True, exist_ok=True)
    target = media_root / f'letter-image-{image_id}{suffix}'
    temporary = target.with_suffix('.part')
    temporary.write_bytes(raw)
    temporary.replace(target)
    return {'image_id': image_id, 'name': target.name, 'width': width, 'height': height}


def attachments(media_root: Path | None, image_ids: object) -> list[str]:
    """File names of the uploads a letter names, in its order. No list means no pictures."""
    if image_ids is None:
        return []
    if (not isinstance(image_ids, list) or len(image_ids) > MAX_IMAGES or len(set(map(str, image_ids))) != len(image_ids)
            or not all(isinstance(item, str) and re.fullmatch('[0-9a-f]{32}', item) for item in image_ids)):
        raise ValueError('LETTER_IMAGES_INVALID')
    names = []
    for image_id in image_ids:
        found = [media_root / f'letter-image-{image_id}{suffix}' for suffix in _SUFFIXES.values()
                 if media_root is not None and (media_root / f'letter-image-{image_id}{suffix}').is_file()]
        if len(found) != 1:
            raise ValueError('LETTER_IMAGE_NOT_FOUND')
        names.append(found[0].name)
    return names


def image_path(media_root: Path | None, name: str) -> Path:
    if media_root is None or not NAME.fullmatch(name):
        raise ValueError('LETTER_IMAGE_NOT_FOUND')
    path = (media_root / name).resolve()
    if not path.is_relative_to(media_root.resolve()) or not path.is_file():
        raise ValueError('LETTER_IMAGE_NOT_FOUND')
    return path
