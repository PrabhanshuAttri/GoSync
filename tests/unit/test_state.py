from pathlib import Path

from gosync.constants import STATUS_DOWNLOADED, STATUS_FAILED, STATUS_PENDING
from gosync.manifest import MediaManifest
from gosync.paths import media_download_path
from gosync.state import (
    completed_count,
    create_or_update_state,
    downloaded_extension_counts,
    downloaded_keys,
    format_downloaded_extension_summary,
    load_state,
    mark_downloaded,
    mark_failed,
    mark_sidecars,
    media_file_exists,
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


def test_create_or_update_state_preserves_existing_record_fields(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    state = create_or_update_state(state_file, manifest_for_state([item]))
    state["media"][item.key]["sidecar_status"] = "complete"
    state["media"][item.key]["retry_count"] = 2
    state["media"][item.key]["last_error"] = "network"
    save_state(state_file, state)

    state = create_or_update_state(state_file, manifest_for_state([item]))

    assert state["media"][item.key]["sidecar_status"] == "complete"
    assert state["media"][item.key]["retry_count"] == 2
    assert state["media"][item.key]["last_error"] == "network"


def test_sync_state_with_downloads_marks_found_and_missing_files(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010002.JPG", 4)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    create_or_update_state(state_file, manifest_for_state([item]))

    media_download_path(downloads, item.filename).parent.mkdir(parents=True)
    media_download_path(downloads, item.filename).write_text("done", encoding="utf-8")
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [{"id": "A", "filename": item.filename, "status": "found"}]
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED

    media_download_path(downloads, item.filename).unlink()
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [{"id": "A", "filename": item.filename, "status": "missing"}]
    assert state["media"][item.key]["download_status"] == STATUS_PENDING


def test_sync_state_with_downloads_finds_existing_flat_media_files(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010002.MP4", 4)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    create_or_update_state(state_file, manifest_for_state([item]))

    downloads.mkdir()
    (downloads / item.filename).write_text("done", encoding="utf-8")
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [{"id": "A", "filename": item.filename, "status": "found"}]
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED


def test_sync_state_with_downloads_finds_case_variant_nested_media_files(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010002.MP4", 4)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    create_or_update_state(state_file, manifest_for_state([item]))

    existing_path = downloads / "MP4" / "gx010002.mp4"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text("done", encoding="utf-8")
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [{"id": "A", "filename": item.filename, "status": "found"}]
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED


def test_sync_state_with_downloads_does_not_mark_size_mismatched_file_downloaded(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010002.MP4", 50)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    create_or_update_state(state_file, manifest_for_state([item]))

    media_download_path(downloads, item.filename).parent.mkdir(parents=True)
    media_download_path(downloads, item.filename).write_text(
        "short", encoding="utf-8"
    )
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == []
    assert state["media"][item.key]["download_status"] == STATUS_PENDING


def test_sync_state_with_downloads_reverts_size_mismatched_downloaded_file(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010002.MP4", 50)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    create_or_update_state(state_file, manifest_for_state([item]))

    media_download_path(downloads, item.filename).parent.mkdir(parents=True)
    media_download_path(downloads, item.filename).write_text(
        "x" * 50, encoding="utf-8"
    )
    state, _ = sync_state_with_downloads(state_file, downloads)
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED

    media_download_path(downloads, item.filename).write_text(
        "truncated", encoding="utf-8"
    )
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == [
        {"id": "A", "filename": item.filename, "status": "size_mismatch"}
    ]
    assert state["media"][item.key]["download_status"] == STATUS_PENDING
    assert "size mismatch" in state["media"][item.key]["last_error"].lower()


def test_media_file_exists_rejects_parent_directory_traversal(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_text("done", encoding="utf-8")

    assert not media_file_exists(downloads, "../outside.mp4")


def test_sync_state_with_downloads_ignores_files_outside_output_dir(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "../outside.mp4", 50)
    state_file = tmp_path / "state.json"
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (tmp_path / "outside.mp4").write_text("done", encoding="utf-8")
    create_or_update_state(state_file, manifest_for_state([item]))

    state, changes = sync_state_with_downloads(state_file, downloads)

    assert changes == []
    assert state["media"][item.key]["download_status"] == STATUS_PENDING


def test_state_markers_update_counts_and_errors(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(state_file, manifest_for_state([item]))

    state = mark_failed(state_file, [item.key], "timeout")
    assert state["media"][item.key]["retry_count"] == 1
    assert state["media"][item.key]["last_error"] == "timeout"
    assert completed_count(state) == 0
    assert pending_keys(state) == {item.key}
    assert downloaded_keys(state) == set()

    state = mark_failed(state_file, [item.key], "gave up", retry=False)
    assert state["media"][item.key]["download_status"] == STATUS_FAILED

    state = mark_downloaded(state_file, [item.key])
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED
    assert state["media"][item.key]["last_error"] == ""
    assert completed_count(state) == 1
    assert pending_keys(state) == set()
    assert downloaded_keys(state) == {item.key}

    mark_sidecars(state_file, [item.key], "complete")
    assert load_state(state_file)["media"][item.key]["sidecar_status"] == "complete"


def test_downloaded_extension_summary_counts_only_downloaded_records(
    tmp_path: Path,
    make_media_item,
) -> None:
    mp4_item = make_media_item("A", "clip.MP4", 10)
    jpg_item = make_media_item("B", "photo.JPG", 10)
    pending_item = make_media_item("C", "pending.mp4", 10)
    state_file = tmp_path / "state.json"
    state = create_or_update_state(
        state_file,
        manifest_for_state([mp4_item, jpg_item, pending_item]),
    )
    state["media"][mp4_item.key]["download_status"] = STATUS_DOWNLOADED
    state["media"][jpg_item.key]["download_status"] = STATUS_DOWNLOADED

    assert downloaded_extension_counts(state) == {"jpg": 1, "mp4": 1}
    assert format_downloaded_extension_summary(state) == (
        "Already downloaded by extension: JPG: 1, MP4: 1 (2 files)"
    )
