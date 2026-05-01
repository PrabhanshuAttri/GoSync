import json
from pathlib import Path
from typing import Any

import pytest

from gosync.manifest import MediaItem


def make_media_item(
    media_id: str = "ABCDEFGHIJKLM",
    filename: str = "GX010001.MP4",
    file_size: int | None = 100,
    **metadata: Any,
) -> MediaItem:
    item_metadata = {
        "id": media_id,
        "filename": filename,
        "file_extension": Path(filename).suffix.lstrip("."),
        "file_size": file_size,
        "content_type": "video/mp4",
        **metadata,
    }
    return MediaItem(
        key=f"{media_id}_{filename}",
        media_id=media_id,
        filename=filename,
        sidecar_filename=f"{filename}.xmp",
        file_size=file_size,
        metadata=item_metadata,
    )


def sample_media_records() -> list[dict[str, Any]]:
    return [
        {
            "id": "ABCDEFGHIJKLM",
            "filename": "GX010001.MP4",
            "file_extension": "MP4",
            "file_size": 100,
            "content_type": "video/mp4",
            "captured_at": "2026-04-01T12:30:00Z",
            "camera_model": "HERO13 Black",
        },
        {
            "id": "NOPQRSTUVWXYZ",
            "filename": "GX010002.JPG",
            "file_extension": "JPG",
            "file_size": 50,
            "content_type": "image/jpeg",
            "width": 5568,
            "height": 4176,
        },
        {
            "id": "UNNAMEDMEDIA1",
            "file_extension": "MP4",
            "file_size": 25,
            "content_type": "video/mp4",
        },
        {
            "id": "ABCDEFGHIJKLM",
            "filename": "GX010001.MP4",
            "file_extension": "MP4",
            "file_size": 100,
            "content_type": "video/mp4",
        },
    ]


def write_har(
    path: Path,
    media: list[dict[str, Any]] | None = None,
    *,
    url: str = "https://api.gopro.com/media/search?page=1",
) -> None:
    path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {"url": url},
                            "response": {
                                "content": {
                                    "text": json.dumps(
                                        {
                                            "_embedded": {
                                                "media": media
                                                if media is not None
                                                else sample_media_records()
                                            }
                                        }
                                    )
                                }
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture(name="make_media_item")
def make_media_item_pytest_fixture():
    return make_media_item


@pytest.fixture
def write_sample_har():
    return write_har
