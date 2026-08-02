import base64
import json
from pathlib import Path

import pytest

from gosync.manifest import (
    build_manifest_from_pages,
    extract_media_items,
    filename_extension,
    format_extension_summary,
    is_media_file,
    json_api_headers,
    parse_response_text,
    read_manifest_from_api,
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


def test_build_manifest_from_pages_dedupes_like_the_har_path() -> None:
    page = (
        "https://api.gopro.com/media/search?page=1",
        [
            {"id": "A", "filename": "a.mp4", "file_extension": "mp4", "file_size": 1},
            {"id": "B", "filename": "b.jpg", "file_extension": "jpg", "file_size": 2},
            {"id": "A", "filename": "a.mp4", "file_extension": "mp4", "file_size": 1},
        ],
    )

    manifest = build_manifest_from_pages([page])

    assert [item.key for item in manifest.media] == ["A_a.mp4", "B_b.jpg"]
    assert len(manifest.duplicates) == 1
    assert manifest.matching_entries == 1


def test_build_manifest_from_pages_rejects_empty_media() -> None:
    with pytest.raises(ValueError, match="No media file metadata"):
        build_manifest_from_pages([("https://api.gopro.com/media/search", [])])


def test_json_api_headers_overrides_accept_but_keeps_authorization() -> None:
    headers = json_api_headers(
        {"Authorization": "Bearer xyz", "Accept": "application/zip"}
    )

    assert headers["Authorization"] == "Bearer xyz"
    assert headers["Accept"] == "application/vnd.gopro.jk.media+json; version=2.0.0"


def test_read_manifest_from_api_paginates_until_last_page(monkeypatch) -> None:
    pages = [
        {
            "_embedded": {
                "media": [
                    {"id": "A", "filename": "a.mp4", "file_extension": "mp4"},
                ]
            },
            "_pages": {"total_pages": 2},
        },
        {
            "_embedded": {
                "media": [
                    {"id": "B", "filename": "b.mp4", "file_extension": "mp4"},
                ]
            },
            "_pages": {"total_pages": 2},
        },
    ]
    requested_pages: list[int] = []

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload
            self.url = "https://api.gopro.com/media/search?page=1"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    def fake_get(url, headers, params, timeout):
        requested_pages.append(params["page"])
        return FakeResponse(pages[params["page"] - 1])

    monkeypatch.setattr("gosync.manifest.requests.get", fake_get)

    manifest = read_manifest_from_api({"Authorization": "Bearer xyz"})

    assert requested_pages == [1, 2]
    assert [item.key for item in manifest.media] == ["A_a.mp4", "B_b.mp4"]
    assert manifest.matching_entries == 2


def test_read_manifest_from_api_scopes_by_user_id(monkeypatch) -> None:
    seen_params: list[dict] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "_embedded": {
                    "media": [
                        {"id": "A", "filename": "a.mp4", "file_extension": "mp4"},
                    ]
                },
                "_pages": {"total_pages": 1},
            }

        url = "https://api.gopro.com/media/search?page=1"

    def fake_get(url, headers, params, timeout):
        seen_params.append(params)
        return FakeResponse()

    monkeypatch.setattr("gosync.manifest.requests.get", fake_get)

    read_manifest_from_api({"Authorization": "Bearer xyz"}, user_id="user-123")

    assert seen_params[0]["gopro_user_id"] == "user-123"
