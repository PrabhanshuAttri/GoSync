import base64
import json
from pathlib import Path

import pytest

from gosync.manifest import (
    extract_media_items,
    filename_extension,
    format_extension_summary,
    is_media_file,
    parse_response_text,
    read_manifest_from_har,
    sidecar_filename,
)


def test_parse_response_text_accepts_plain_json() -> None:
    assert parse_response_text('{"ok": true}', 1) == {"ok": True}


def test_parse_response_text_accepts_xssi_prefixed_json() -> None:
    assert parse_response_text(')]}\',\n{"ok": true}', 1) == {"ok": True}


def test_parse_response_text_accepts_base64_json() -> None:
    encoded = base64.b64encode(b'{"ok": true}').decode("ascii")

    assert parse_response_text(encoded, 1) == {"ok": True}


def test_is_media_file_rejects_non_media_records() -> None:
    assert is_media_file(
        {
            "id": "A",
            "filename": "GX010001.MP4",
            "file_extension": "MP4",
            "content_type": "video/mp4",
        }
    )
    assert not is_media_file({"type": "folder", "filename": "Trip"})
    assert not is_media_file({"id": "B", "file_extension": "MP4"})


def test_extract_media_items_uses_known_media_lists_before_recursive_scan() -> None:
    response = {
        "_embedded": {
            "media": [
                {"id": "A", "filename": "a.mp4", "file_extension": "mp4"},
                {"id": "folder", "filename": "Folder", "type": "folder"},
            ]
        },
        "nested": {
            "media": [
                {"id": "B", "filename": "b.jpg", "file_extension": "jpg"},
            ]
        },
    }

    assert [item["id"] for item in extract_media_items(response)] == ["A", "folder"]


def test_extract_media_items_recurses_when_known_lists_are_absent() -> None:
    response = {
        "deep": {
            "items": {
                "one": {"id": "A", "filename": "a.mp4", "file_extension": "mp4"}
            }
        }
    }

    assert [item["id"] for item in extract_media_items(response)] == ["A"]


def test_sidecar_filename_does_not_duplicate_existing_extension() -> None:
    assert (
        sidecar_filename({"filename": "GX010001.MP4", "file_extension": "MP4"})
        == "GX010001.MP4.xmp"
    )
    assert (
        sidecar_filename({"filename": "GX010001", "file_extension": "MP4"})
        == "GX010001.MP4.xmp"
    )


def test_filename_extension_falls_back_for_extensionless_names() -> None:
    assert filename_extension("GX010001.MP4") == "mp4"
    assert filename_extension("README") == "no extension"


def test_manifest_deduplicates_and_generates_stable_unnamed_filenames(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_sample_har(har_path)

    manifest = read_manifest_from_har(har_path)

    assert [item.key for item in manifest.media] == [
        "ABCDEFGHIJKLM_GX010001.MP4",
        "NOPQRSTUVWXYZ_GX010002.JPG",
        "UNNAMEDMEDIA1_unnamed_1.MP4",
    ]
    assert len(manifest.duplicates) == 1
    assert manifest.duplicates[0].key == "ABCDEFGHIJKLM_GX010001.MP4"
    assert (
        format_extension_summary(manifest)
        == "HAR Media by extension: JPG: 1, MP4: 2"
    )
    assert manifest.media_responses[0]["media"][2]["filename"] == "unnamed_1.MP4"


def test_read_manifest_rejects_invalid_har_structure(tmp_path: Path) -> None:
    har_path = tmp_path / "bad.har"
    har_path.write_text(json.dumps({"log": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing log.entries"):
        read_manifest_from_har(har_path)


def test_read_manifest_requires_media_search_entries(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "wrong.har"
    write_sample_har(har_path, url="https://example.com/not-gopro")

    with pytest.raises(ValueError, match="No API calls"):
        read_manifest_from_har(har_path)
