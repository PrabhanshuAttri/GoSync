from pathlib import Path

from gosync.constants import (
    DEFAULT_LEGACY_COMPLETED_LOG,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_PENDING,
)
from gosync.manifest import MediaManifest
from gosync.paths import media_download_path
from gosync.state import (
    completed_count,
    create_or_update_state,
    load_state,
    mark_downloaded,
    mark_failed,
    mark_sidecars,
    pending_keys,
    save_state,
    sync_state_with_downloads,
)


def manifest_for_state(items) -> MediaManifest:
    return MediaManifest(
        media=items,
        duplicates=[],
        matching_entries=1,
        media_responses=[],
    )


def test_load_state_returns_empty_default_for_missing_file(tmp_path: Path) -> None:
    state = load_state(tmp_path / "missing.json")

    assert state["version"] == 1
    assert state["media"] == {}


def test_create_or_update_state_imports_legacy_completed_ids_once(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "done.mp4", 10)
    state_file = tmp_path / "state.json"
    (tmp_path / DEFAULT_LEGACY_COMPLETED_LOG).write_text("A,", encoding="utf-8")

    state = create_or_update_state(state_file, manifest_for_state([item]), tmp_path)

    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED

    state["media"][item.key]["download_status"] = STATUS_PENDING
    save_state(state_file, state)
    state = create_or_update_state(state_file, manifest_for_state([item]), tmp_path)

    assert state["media"][item.key]["download_status"] == STATUS_PENDING


def test_create_or_update_state_preserves_existing_record_fields(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    state = create_or_update_state(state_file, manifest_for_state([item]), tmp_path)
    state["media"][item.key]["sidecar_status"] = "complete"
    state["media"][item.key]["retry_count"] = 2
    state["media"][item.key]["last_error"] = "network"
    save_state(state_file, state)

    state = create_or_update_state(state_file, manifest_for_state([item]), tmp_path)

    assert state["media"][item.key]["sidecar_status"] == "complete"
    assert state["media"][item.key]["retry_count"] == 2
    assert state["media"][item.key]["last_error"] == "network"


def test_sync_state_with_downloads_marks_found_and_missing_files(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010002.JPG", 50)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    create_or_update_state(state_file, manifest_for_state([item]), tmp_path)

    media_download_path(downloads, item.filename).parent.mkdir(parents=True)
    media_download_path(downloads, item.filename).write_text("done", encoding="utf-8")
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [{"id": "A", "filename": item.filename, "status": "found"}]
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED

    media_download_path(downloads, item.filename).unlink()
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [{"id": "A", "filename": item.filename, "status": "missing"}]
    assert state["media"][item.key]["download_status"] == STATUS_PENDING


def test_state_markers_update_counts_and_errors(tmp_path: Path, make_media_item) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(state_file, manifest_for_state([item]), tmp_path)

    state = mark_failed(state_file, [item.key], "timeout")
    assert state["media"][item.key]["retry_count"] == 1
    assert state["media"][item.key]["last_error"] == "timeout"
    assert completed_count(state) == 0
    assert pending_keys(state) == {item.key}

    state = mark_failed(state_file, [item.key], "gave up", retry=False)
    assert state["media"][item.key]["download_status"] == STATUS_FAILED

    state = mark_downloaded(state_file, [item.key])
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED
    assert state["media"][item.key]["last_error"] == ""
    assert completed_count(state) == 1
    assert pending_keys(state) == set()

    mark_sidecars(state_file, [item.key], "complete")
    assert load_state(state_file)["media"][item.key]["sidecar_status"] == "complete"

