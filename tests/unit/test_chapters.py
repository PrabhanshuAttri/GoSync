import shutil
import subprocess
from pathlib import Path

from gosync.chapters import (
    find_chapter_source_files,
    parse_chapter_filename,
    size_matches,
    tolerance_bytes,
    validate_chapter_integrity,
)


def test_parse_chapter_filename_orders_modern_chapter_names() -> None:
    assert parse_chapter_filename("GX010320.MP4") == ("0320", 1)
    assert parse_chapter_filename("gx020320.mp4") == ("0320", 2)
    assert parse_chapter_filename("vacation.mp4") is None


def test_parse_chapter_filename_supports_gopr_legacy_first_chapter() -> None:
    assert parse_chapter_filename("GOPR0320.MP4") == ("0320", 0)
    assert parse_chapter_filename("GP020320.MP4") == ("0320", 2)


def test_find_chapter_source_files_orders_by_chapter_number(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name in ("GX030320.MP4", "GX010320.MP4", "GX020320.MP4"):
        (output_dir / name).write_text(name, encoding="utf-8")

    result = find_chapter_source_files(output_dir, item)

    assert result == [
        output_dir / "GX010320.MP4",
        output_dir / "GX020320.MP4",
        output_dir / "GX030320.MP4",
    ]


def test_find_chapter_source_files_excludes_other_extensions_and_groups(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010320.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020320.MP4").write_text("chapter-2", encoding="utf-8")
    (output_dir / "GL010320.LRV").write_text("proxy", encoding="utf-8")
    (output_dir / "GX010500.MP4").write_text("other recording", encoding="utf-8")

    result = find_chapter_source_files(output_dir, item)

    assert result == [output_dir / "GX010320.MP4", output_dir / "GX020320.MP4"]


def test_find_chapter_source_files_returns_none_when_fewer_than_two_present(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010320.MP4").write_text("chapter-1", encoding="utf-8")

    assert find_chapter_source_files(output_dir, item) is None


def test_find_chapter_source_files_includes_original_unmerged_directory(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    originals_dir = output_dir / "original_unmerged_mp4"
    originals_dir.mkdir(parents=True)
    for name in ("GX010320.MP4", "GX020320.MP4", "GX030320.MP4"):
        (originals_dir / name).write_text(name, encoding="utf-8")

    result = find_chapter_source_files(output_dir, item)

    assert result == [
        originals_dir / "GX010320.MP4",
        originals_dir / "GX020320.MP4",
        originals_dir / "GX030320.MP4",
    ]


def test_find_chapter_source_files_dedupes_chapter_present_in_both_locations(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    originals_dir = output_dir / "original_unmerged_mp4"
    originals_dir.mkdir(parents=True)
    (output_dir / "GX010320.MP4").write_text("flat-1", encoding="utf-8")
    (output_dir / "GX020320.MP4").write_text("flat-2", encoding="utf-8")
    (originals_dir / "GX020320.MP4").write_text("originals-2", encoding="utf-8")
    (originals_dir / "GX030320.MP4").write_text("originals-3", encoding="utf-8")

    result = find_chapter_source_files(output_dir, item)

    assert result is not None
    assert len(result) == 3
    assert {path.name for path in result} == {
        "GX010320.MP4",
        "GX020320.MP4",
        "GX030320.MP4",
    }


def test_tolerance_bytes_scales_with_chapter_count_and_is_capped(monkeypatch) -> None:
    monkeypatch.setattr("gosync.chapters.PER_CHAPTER_OVERHEAD_BYTES", 10)
    monkeypatch.setattr("gosync.chapters.MIN_TOLERANCE_BYTES", 15)
    monkeypatch.setattr("gosync.chapters.MAX_TOLERANCE_BYTES", 55)

    # Below the floor: MIN_TOLERANCE_BYTES wins.
    assert tolerance_bytes(1) == 15
    # In between: chapter_count * PER_CHAPTER_OVERHEAD_BYTES wins.
    assert tolerance_bytes(3) == 30
    # Above the ceiling: MAX_TOLERANCE_BYTES wins.
    assert tolerance_bytes(10) == 55


def test_tolerance_bytes_matches_real_measured_shrinkage_at_defaults() -> None:
    # Regression guard for the real-world numbers this model was built from
    # (scripts/measure_chapter_merge_size_delta.py): a 7-chapter merge that
    # shrank by ~10 MiB relative to the raw chapter-sum must still validate
    # at the *default* (non-monkeypatched) tolerance constants.
    measured_shrink_bytes = 10 * 1024 * 1024
    assert size_matches(
        100_000_000 - measured_shrink_bytes, 100_000_000, item_count=7
    )


def test_size_matches_requires_exact_equality_for_non_chaptered_items() -> None:
    assert size_matches(1000, 1000, item_count=1) is True
    assert size_matches(999, 1000, item_count=1) is False
    assert size_matches(1000, 1000) is True  # item_count defaults to 1


def test_size_matches_none_expected_size_always_matches() -> None:
    assert size_matches(12345, None, item_count=1) is True
    assert size_matches(12345, None, item_count=3) is True


def test_size_matches_applies_symmetric_tolerance_for_chaptered_items(
    monkeypatch,
) -> None:
    monkeypatch.setattr("gosync.chapters.PER_CHAPTER_OVERHEAD_BYTES", 5)
    monkeypatch.setattr("gosync.chapters.MIN_TOLERANCE_BYTES", 5)
    monkeypatch.setattr("gosync.chapters.MAX_TOLERANCE_BYTES", 1000)

    # tolerance_bytes(2) == 10 with the monkeypatched constants above.
    assert size_matches(1010, 1000, item_count=2) is True  # grew, within band
    assert size_matches(990, 1000, item_count=2) is True  # shrank, within band
    assert size_matches(1011, 1000, item_count=2) is False  # grew, beyond band
    assert size_matches(989, 1000, item_count=2) is False  # shrank, beyond band


def test_validate_chapter_integrity_rejects_empty_list() -> None:
    assert validate_chapter_integrity([]) is False


def test_validate_chapter_integrity_rejects_zero_byte_chapter(tmp_path: Path) -> None:
    good = tmp_path / "chapter1.mp4"
    good.write_bytes(b"x" * 1000)
    empty = tmp_path / "chapter2.mp4"
    empty.write_bytes(b"")

    assert validate_chapter_integrity([good, empty]) is False


def test_validate_chapter_integrity_rejects_size_outlier(tmp_path: Path) -> None:
    good = tmp_path / "chapter1.mp4"
    good.write_bytes(b"x" * 10_000)
    truncated = tmp_path / "chapter2.mp4"
    truncated.write_bytes(b"x" * 10)  # far under 10% of the sibling's size

    assert validate_chapter_integrity([good, truncated]) is False


def test_validate_chapter_integrity_accepts_similarly_sized_chapters_without_ffprobe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _binary: None)
    chapter1 = tmp_path / "chapter1.mp4"
    chapter1.write_bytes(b"x" * 1000)
    chapter2 = tmp_path / "chapter2.mp4"
    chapter2.write_bytes(b"x" * 1010)

    assert validate_chapter_integrity([chapter1, chapter2]) is True


def test_validate_chapter_integrity_uses_ffprobe_when_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chapter1 = tmp_path / "chapter1.mp4"
    chapter1.write_bytes(b"x" * 1000)
    chapter2 = tmp_path / "chapter2.mp4"
    chapter2.write_bytes(b"x" * 1000)

    def fake_which(binary: str) -> str | None:
        return "/usr/bin/ffprobe" if binary == "ffprobe" else None

    probed: list[str] = []

    def fake_run(cmd, **_kwargs):
        probed.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout="1.5", stderr="")

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert validate_chapter_integrity([chapter1, chapter2]) is True
    assert probed == [str(chapter1), str(chapter2)]


def test_validate_chapter_integrity_rejects_when_ffprobe_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    chapter1 = tmp_path / "chapter1.mp4"
    chapter1.write_bytes(b"x" * 1000)
    chapter2 = tmp_path / "chapter2.mp4"
    chapter2.write_bytes(b"x" * 1000)

    def fake_which(binary: str) -> str | None:
        return "/usr/bin/ffprobe" if binary == "ffprobe" else None

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unreadable")

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert validate_chapter_integrity([chapter1, chapter2]) is False
