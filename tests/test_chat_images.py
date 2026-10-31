import base64

import pytest

from src.apps.chat import images as images_module
from src.apps.chat.images import InvalidImageError, normalize

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


def data_uri(raw: bytes = PNG, mime: str = 'image/png') -> str:
    return f'data:{mime};base64,' + base64.b64encode(raw).decode()


def test_a_valid_image_is_forwarded_untouched():
    uri = data_uri()
    assert normalize([uri]) == [uri]


def test_an_absent_field_is_an_empty_list():
    assert normalize(None) == []
    assert normalize([]) == []


def test_a_non_list_is_rejected():
    with pytest.raises(InvalidImageError):
        normalize('data:image/png;base64,AAAA')


def test_a_plain_url_is_rejected():
    with pytest.raises(InvalidImageError, match='data URI'):
        normalize(['https://example.com/a.png'])


def test_an_unsupported_type_is_rejected():
    with pytest.raises(InvalidImageError, match='unsupported image type'):
        normalize([data_uri(mime='image/svg+xml')])


def test_a_format_that_does_not_match_its_label_is_rejected():
    with pytest.raises(InvalidImageError, match='declared'):
        normalize([data_uri(mime='image/jpeg')])


def test_an_oversized_image_is_rejected(monkeypatch):
    monkeypatch.setattr(images_module, 'IMAGE_MAX_BYTES', 8)
    with pytest.raises(InvalidImageError, match='larger than'):
        normalize([data_uri()])


def test_too_many_images_are_rejected(monkeypatch):
    monkeypatch.setattr(images_module, 'IMAGE_MAX_PER_MESSAGE', 1)
    with pytest.raises(InvalidImageError, match='at most 1'):
        normalize([data_uri(), data_uri()])


def test_the_offending_image_is_named():
    with pytest.raises(InvalidImageError, match='image 2:'):
        normalize([data_uri(), 'nope'])
