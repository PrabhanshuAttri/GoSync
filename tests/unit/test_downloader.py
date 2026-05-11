import zipfile
from pathlib import Path

import pytest

from gosync.downloader import (
    build_size_batches,
    format_media_for_log,
    format_size_mib,
    organize_extracted_media,
    parse_batch_file_limit,
    parse_batch_max_bytes,
    process_pipeline,
    resolve_har_file,
    safe_extract,
)
from gosync.manifest import MediaManifest
from gosync.paths import media_download_path
from gosync.progress import ProgressState
from gosync.state import create_or_update_state, mark_downloaded


def test_size_batches_use_largest_file_as_auto_cap(make_media_item) -> None:
    items = [
        make_media_item("A", "large.mp4", 100),
        make_media_item("B", "medium.mp4", 60),
        make_media_item("C", "small.jpg", 40),
        make_media_item("D", "tiny.jpg", 10),
    ]

    batch_cap = parse_batch_max_bytes("auto", items)
    batches = build_size_batches(items, batch_cap)

    assert batch_cap == 100
    assert [[item.filename for item in batch] for batch in batches] == [
        ["large.mp4"],
        ["medium.mp4", "small.jpg"],
        ["tiny.jpg"],
    ]


def test_size_batches_clamp_oversized_cap_to_largest_file(make_media_item) -> None:
    items = [
        make_media_item("A", "largest.mp4", 80),
        make_media_item("B", "large.mp4", 50),
        make_media_item("C", "medium.mp4", 40),
        make_media_item("D", "small.jpg", 30),
    ]

    batch_cap = parse_batch_max_bytes(200, items)
    batches = build_size_batches(items, 200)

    assert batch_cap == 80
    assert [[item.filename for item in batch] for batch in batches] == [
        ["largest.mp4"],
        ["large.mp4", "small.jpg"],
        ["medium.mp4"],
    ]
    assert all(
        sum(item.file_size or 0 for item in batch) <= 80 for batch in batches
    )


def test_size_batches_respect_files_per_batch_limit(make_media_item) -> None:
    items = [
        make_media_item("A", "largest.mp4", 100),
        make_media_item("B", "one.mp4", 30),
        make_media_item("C", "two.mp4", 25),
        make_media_item("D", "three.jpg", 20),
        make_media_item("E", "four.jpg", 15),
    ]

    batches = build_size_batches(items, 100, batch_file_limit=2)

    assert [[item.filename for item in batch] for batch in batches] == [
        ["largest.mp4"],
        ["one.mp4", "two.mp4"],
        ["three.jpg", "four.jpg"],
    ]


def test_process_pipeline_uses_full_manifest_for_auto_batch_cap(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    largest = make_media_item("A", "huge.mp4", 100)
    selected_items = [
        make_media_item("B", "small-three.mp4", 3),
        make_media_item("C", "small-two.mp4", 2),
        make_media_item("D", "small-one.jpg", 1),
    ]
    full_manifest_items = [largest, *selected_items]
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=full_manifest_items,
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )
    downloaded_batches = []

    def fake_download_batch(
        _session,
        batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
    ):
        downloaded_batches.append(batch)
        with zipfile.ZipFile(temp_zip, "w"):
            pass

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)
    monkeypatch.setattr("gosync.downloader.organize_extracted_media", lambda *_: None)

    process_pipeline(
        media_items=selected_items,
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
        batch_file_limit=3,
        batch_cap_media_items=full_manifest_items,
    )

    assert downloaded_batches == [["B", "C", "D"]]


def test_process_pipeline_counts_progress_for_active_items_only(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    already_done = make_media_item("A", "already.mp4", 100)
    selected = make_media_item("B", "selected.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[already_done, selected],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )
    mark_downloaded(state_file, [already_done.key])
    progress = ProgressState(job_id="job-1")

    def fake_download_batch(
        _session,
        _batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
    ):
        with zipfile.ZipFile(temp_zip, "w"):
            pass

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)
    monkeypatch.setattr("gosync.downloader.organize_extracted_media", lambda *_: None)

    process_pipeline(
        media_items=[selected],
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
        progress=progress,
        job_id="job-1",
    )

    snapshot = progress.snapshot()
    assert snapshot["total_ids"] == 1
    assert snapshot["completed_ids"] == 1
    assert snapshot["pending_ids"] == 0


def test_size_batches_put_unknown_size_items_in_single_item_batches(
    make_media_item,
) -> None:
    items = [
        make_media_item("A", "known.mp4", 10),
        make_media_item("B", "unknown.mp4", None),
    ]

    batches = build_size_batches(items, 10)

    assert [[item.filename for item in batch] for batch in batches] == [
        ["known.mp4"],
        ["unknown.mp4"],
    ]


@pytest.mark.parametrize("value", ["0", 0, "-1", "not-a-number"])
def test_parse_batch_max_bytes_rejects_invalid_values(value, make_media_item) -> None:
    with pytest.raises(ValueError):
        parse_batch_max_bytes(value, [make_media_item()])


@pytest.mark.parametrize("value", ["1", 1, "12"])
def test_parse_batch_file_limit_accepts_positive_values(value) -> None:
    assert parse_batch_file_limit(value) == int(value)


@pytest.mark.parametrize("value", ["0", 0, "-1", "not-a-number"])
def test_parse_batch_file_limit_rejects_invalid_values(value) -> None:
    with pytest.raises(ValueError):
        parse_batch_file_limit(value)


def test_format_media_size_uses_binary_units(make_media_item) -> None:
    item = make_media_item("A", "large.mp4", 10 * 1024 * 1024)
    larger_item = make_media_item("C", "larger.mp4", 1536 * 1024 * 1024)

    assert format_size_mib(512) == "512.00 B"
    assert format_size_mib(1024) == "1.00 KiB"
    assert format_size_mib(1023 * 1024) == "1023.00 KiB"
    assert format_size_mib(1024 * 1024) == "1.00 MiB"
    assert format_size_mib(1024 * 1024 * 1024) == "1.00 GiB"
    assert format_size_mib(1024 * 1024 * 1024 * 1024) == "1.00 TiB"
    assert format_size_mib(item.file_size) == "10.00 MiB"
    assert format_media_for_log(item) == "large.mp4 (A, 10.00 MiB)"
    assert format_size_mib(larger_item.file_size) == "1.50 GiB"
    assert format_media_for_log(larger_item) == "larger.mp4 (C, 1.50 GiB)"
    assert format_size_mib(None) == "unknown size"


def test_resolve_har_file_rejects_parent_directory_traversal(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside_har = tmp_path / "outside.har"
    outside_har.write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="must be a filename"):
        resolve_har_file(data_dir, "../outside.har")


def test_resolve_har_file_rejects_absolute_paths_outside_data_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside_har = tmp_path / "outside.har"
    outside_har.write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="must be a filename"):
        resolve_har_file(data_dir, str(outside_har))


def test_resolve_har_file_rejects_nested_paths_inside_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    nested = data_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.har").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="must be a filename"):
        resolve_har_file(data_dir, "nested/inside.har")


def test_resolve_har_file_rejects_non_har_extension(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "inside.txt").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="must use the .har extension"):
        resolve_har_file(data_dir, "inside.txt")


def test_resolve_har_file_allows_har_filename_inside_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    har_path = data_dir / "inside.har"
    har_path.write_text("{}", encoding="utf-8")

    assert resolve_har_file(data_dir, "inside.har") == har_path.resolve()


def test_organize_extracted_media_moves_files_into_extension_dirs(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "large.MP4", 10)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "large.MP4").write_text("media", encoding="utf-8")

    organize_extracted_media(output_dir, [item])

    assert not (output_dir / "large.MP4").exists()
    assert media_download_path(output_dir, "large.MP4").read_text(
        encoding="utf-8"
    ) == "media"


def test_organize_extracted_media_keeps_existing_target(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "large.MP4", 10)
    output_dir = tmp_path / "downloads"
    target = media_download_path(output_dir, "large.MP4")
    target.parent.mkdir(parents=True)
    target.write_text("already done", encoding="utf-8")
    (output_dir / "large.MP4").write_text("new media", encoding="utf-8")

    organize_extracted_media(output_dir, [item])

    assert target.read_text(encoding="utf-8") == "already done"
    assert (output_dir / "large.MP4").read_text(encoding="utf-8") == "new media"


def test_organize_extracted_media_ignores_source_paths_outside_output_dir(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "../outside.MP4", 10)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    outside_path = tmp_path / "outside.MP4"
    outside_path.write_text("outside", encoding="utf-8")

    organize_extracted_media(output_dir, [item])

    assert outside_path.read_text(encoding="utf-8") == "outside"
    assert not media_download_path(output_dir, item.filename).exists()


def test_safe_extract_rejects_zip_slip_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr("../escape.txt", "bad")

    with zipfile.ZipFile(archive_path) as zip_ref, pytest.raises(ValueError):
        safe_extract(zip_ref, tmp_path / "downloads")
