import base64
import binascii
import re

from rqark_core.func.image import detect_mime

from src.config import IMAGE_MAX_BYTES, IMAGE_MAX_PER_MESSAGE

ALLOWED_MIME = frozenset({'image/jpeg', 'image/png', 'image/webp', 'image/gif'})

_DATA_URI = re.compile(r'^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>[A-Za-z0-9+/=]+)$')


class InvalidImageError(ValueError):
    """A frame carried an image the cloud will not forward to the model."""


def _check(uri: str) -> str:
    match = _DATA_URI.match(uri)
    if match is None:
        raise InvalidImageError('images must be base64 data URIs')
    declared = match.group('mime').lower()
    if declared not in ALLOWED_MIME:
        raise InvalidImageError(f'unsupported image type: {declared}')
    try:
        raw = base64.b64decode(match.group('data'), validate=True)
    except binascii.Error as error:
        raise InvalidImageError('image payload is not valid base64') from error
    if len(raw) > IMAGE_MAX_BYTES:
        raise InvalidImageError(f'image is larger than {IMAGE_MAX_BYTES} bytes')
    try:
        detected = detect_mime(raw[:32])
    except ValueError as error:
        raise InvalidImageError('image content is not a recognised format') from error
    if detected != declared:
        raise InvalidImageError(f'image is {detected} but declared {declared}')
    return uri


def normalize(images: object) -> list[str]:
    """Validate an `images` field from the wire; raise InvalidImageError on the first bad one."""
    if images is None:
        return []
    if not isinstance(images, list):
        raise InvalidImageError('images must be a list')
    if len(images) > IMAGE_MAX_PER_MESSAGE:
        raise InvalidImageError(f'at most {IMAGE_MAX_PER_MESSAGE} images per message')
    checked: list[str] = []
    for number, uri in enumerate(images, start=1):
        if not isinstance(uri, str):
            raise InvalidImageError(f'image {number} is not a string')
        try:
            checked.append(_check(uri))
        except InvalidImageError as error:
            raise InvalidImageError(f'image {number}: {error}') from error
    return checked
