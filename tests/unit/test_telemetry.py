from pathlib import Path

from gosync.constants import STATUS_COMPLETE, STATUS_FAILED
from gosync.state import create_or_update_state, load_state
from gosync.telemetry import (
    download_gpx_or_gpmf,
    download_mediainfo,
    first_gps_fix,
    media_stem,
    merge_gpx,
    run_telemetry_job,
)


def _gpx(
    points: list[tuple[float, float, float | None, str | None]],
    name: str,
) -> bytes:
    trkpts = ""
    for lat, lon, ele, fix in points:
        ele_xml = f"<ele>{ele}</ele>" if ele is not None else ""
        fix_xml = f"<fix>{fix}</fix>" if fix else ""
        trkpts += f'<trkpt lat="{lat}" lon="{lon}">{ele_xml}{fix_xml}</trkpt>'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="GoPro">
  <metadata><name>{name}</name></metadata>
  <trk><name>{name}</name><trkseg>{trkpts}</trkseg></trk>
</gpx>""".encode()


class FakeResponse:
    def __init__(self, content: bytes = b"", json_data: dict | None = None) -> None:
        self.content = content
        self._json = json_data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._json


def test_merge_gpx_combines_all_chapters_into_one_track() -> None:
    chapter1 = _gpx([(1.0, 2.0, 10, None)], "chapter1")
    chapter2 = _gpx([(1.1, 2.1, 11, "3d")], "chapter1")

    merged = merge_gpx([chapter1, chapter2], "combined")

    assert merged.count(b"<trkpt") == 2
    assert b"combined" in merged


def test_first_gps_fix_requires_3d_fix() -> None:
    no_fix = _gpx([(0.0, 0.0, 0, None)], "c1")
    two_d = _gpx([(9.9, 9.9, 0, "2d")], "c1")
    three_d = _gpx([(1.5, 2.5, 12.5, "3d")], "c1")

    assert first_gps_fix([no_fix, two_d]) is None
    assert first_gps_fix([no_fix, two_d, three_d]) == (1.5, 2.5, 12.5)


def test_download_gpx_or_gpmf_prefers_gpx_and_merges_chapters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chapter1 = _gpx([(1.0, 2.0, 10, None)], "clip")
    chapter2 = _gpx([(1.1, 2.1, 11, "3d")], "clip")

    def fake_get(url, headers=None, timeout=None):
        content = chapter1 if "1" in url else chapter2
        return FakeResponse(content=content)

    monkeypatch.setattr("gosync.telemetry.requests.get", fake_get)

    download_data = {
        "_embedded": {
            "sidecar_files": [
                {"label": "gpx", "item_number": 2, "url": "https://x/2.gpx"},
                {"label": "gpx", "item_number": 1, "url": "https://x/1.gpx"},
                {"label": "gpmf", "item_number": 1, "url": "https://x/1.gpmf"},
            ]
        }
    }

    geo = download_gpx_or_gpmf(download_data, tmp_path, "clip")

    gpx_path = tmp_path / "gpx" / "clip.gpx"
    assert gpx_path.exists()
    assert gpx_path.read_bytes().count(b"<trkpt") == 2
    assert geo == (1.1, 2.1, 11.0)
    assert not (tmp_path / "gpmf" / "clip_1.gpmf").exists()


def test_download_gpx_or_gpmf_falls_back_to_gpmf_when_no_gpx(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "gosync.telemetry.requests.get",
        lambda url, headers=None, timeout=None: FakeResponse(content=b"raw-gpmf-bytes"),
    )
    download_data = {
        "_embedded": {
            "sidecar_files": [
                {"label": "gpmf", "item_number": 1, "url": "https://x/1.gpmf"},
                {"label": "gpmf", "item_number": 2, "url": "https://x/2.gpmf"},
            ]
        }
    }

    geo = download_gpx_or_gpmf(download_data, tmp_path, "clip")

    assert geo is None
    assert (tmp_path / "gpmf" / "clip_1.gpmf").read_bytes() == b"raw-gpmf-bytes"
    assert (tmp_path / "gpmf" / "clip_2.gpmf").read_bytes() == b"raw-gpmf-bytes"


def test_download_gpx_or_gpmf_returns_none_when_no_sidecars(tmp_path: Path) -> None:
    result = download_gpx_or_gpmf(
        {"_embedded": {"sidecar_files": []}}, tmp_path, "clip"
    )
    assert result is None


def test_download_mediainfo_strips_sensitive_fields_and_redacts_folder_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/media/ABC123"):
            return FakeResponse(
                json_data={
                    "id": "ABC123",
                    "token": "secret-token",
                    "user_id": "user-999",
                    "gopro_user_id": "user-999",
                    "folder_path": "/accounts/user-999/media",
                }
            )
        return FakeResponse(json_data={"some": "mediainfo"})

    monkeypatch.setattr("gosync.telemetry.requests.get", fake_get)

    download_data = {
        "_embedded": {
            "sidecar_files": [
                {"label": "mediainfo", "url": "https://x/mediainfo.json"},
            ]
        }
    }

    download_mediainfo(
        {"Authorization": "Bearer abc"},
        "ABC123",
        download_data,
        tmp_path,
        "clip",
        geo=(1.0, 2.0, 3.0),
    )

    payload = (tmp_path / "json" / "clip_mediainfo.json").read_text(encoding="utf-8")
    assert "secret-token" not in payload
    assert "user-999" not in payload
    assert "{user_id}" in payload
    assert '"latitude": 1.0' in payload
    assert '"mediainfo"' not in payload or "some" in payload


def test_media_stem_strips_extension() -> None:
    assert media_stem("GX010001.MP4") == "GX010001"


def test_run_telemetry_job_skips_items_already_marked_complete(
    tmp_path: Path,
    monkeypatch,
    make_media_item,
) -> None:
    items = [make_media_item("A", "a.mp4"), make_media_item("B", "b.mp4")]
    state_file = tmp_path / "state.json"
    from gosync.manifest import MediaManifest

    create_or_update_state(
        state_file,
        MediaManifest(
            media=items, duplicates=[], matching_entries=1, media_responses=[]
        ),
    )
    state = load_state(state_file)
    state["media"][items[0].key]["telemetry_status"] = STATUS_COMPLETE
    from gosync.state import save_state

    save_state(state_file, state)

    fetched: list[str] = []
    monkeypatch.setattr(
        "gosync.telemetry.fetch_item_telemetry",
        lambda headers, item, output_dir: fetched.append(item.media_id),
    )

    written, failed = run_telemetry_job(
        {"Authorization": "Bearer abc"},
        items,
        tmp_path / "downloads",
        state_file=state_file,
    )

    assert fetched == ["B"]
    assert written == 1
    assert failed == 0
    final_state = load_state(state_file)
    assert final_state["media"][items[1].key]["telemetry_status"] == STATUS_COMPLETE


def test_run_telemetry_job_marks_failed_on_error(
    tmp_path: Path,
    monkeypatch,
    make_media_item,
) -> None:
    items = [make_media_item("A", "a.mp4")]
    state_file = tmp_path / "state.json"
    from gosync.manifest import MediaManifest

    create_or_update_state(
        state_file,
        MediaManifest(
            media=items, duplicates=[], matching_entries=1, media_responses=[]
        ),
    )

    def failing_fetch(headers, item, output_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr("gosync.telemetry.fetch_item_telemetry", failing_fetch)

    written, failed = run_telemetry_job(
        {"Authorization": "Bearer abc"},
        items,
        tmp_path / "downloads",
        state_file=state_file,
    )

    assert written == 0
    assert failed == 1
    final_state = load_state(state_file)
    record = final_state["media"][items[0].key]
    assert record["telemetry_status"] == STATUS_FAILED
    assert "boom" in record["last_error"]


def test_run_telemetry_job_without_state_file_processes_all(
    tmp_path: Path,
    monkeypatch,
    make_media_item,
) -> None:
    items = [make_media_item("A", "a.mp4"), make_media_item("B", "b.mp4")]
    fetched: list[str] = []
    monkeypatch.setattr(
        "gosync.telemetry.fetch_item_telemetry",
        lambda headers, item, output_dir: fetched.append(item.media_id),
    )

    written, failed = run_telemetry_job(
        {"Authorization": "Bearer abc"},
        items,
        tmp_path / "downloads",
    )

    assert fetched == ["A", "B"]
    assert written == 2
    assert failed == 0
