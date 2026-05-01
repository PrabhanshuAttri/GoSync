import zipfile
from pathlib import Path

import pytest

from gosync.downloader import (
    build_size_batches,
    format_media_for_log,
    format_size_mib,
    organize_extracted_media,
    parse_batch_max_bytes,
    safe_extract,
)
from gosync.paths import media_download_path


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


def test_format_media_size_uses_gib_only_above_1024_mib(make_media_item) -> None:
    item = make_media_item("A", "large.mp4", 10 * 1024 * 1024)
    exact_gib = make_media_item("B", "exact.mp4", 1024 * 1024 * 1024)
    larger_item = make_media_item("C", "larger.mp4", 1536 * 1024 * 1024)

    assert format_size_mib(item.file_size) == "10.00 MiB"
    assert format_media_for_log(item) == "large.mp4 (A, 10.00 MiB)"
    assert format_size_mib(exact_gib.file_size) == "1024.00 MiB"
    assert format_size_mib(larger_item.file_size) == "1.50 GiB"
    assert format_media_for_log(larger_item) == "larger.mp4 (C, 1.50 GiB)"
    assert format_size_mib(None) == "unknown size"


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


def test_safe_extract_rejects_zip_slip_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as zip_ref:
        zip_ref.writestr("../escape.txt", "bad")

    with zipfile.ZipFile(archive_path) as zip_ref, pytest.raises(ValueError):
        safe_extract(zip_ref, tmp_path / "downloads")
