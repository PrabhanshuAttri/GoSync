import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

from gosync.constants import (
    DEFAULT_HEADERS,
    DEFAULT_TEMP_ZIP,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    ZIP_URL_PREFIX,
)
from gosync.downloader import (
    DownloadCancelled,
    build_size_batches,
    download_batch,
    extract_browser_headers,
    find_chapter_source_files,
    format_media_for_log,
    format_size_mib,
    merge_chapter_files,
    organize_extracted_media,
    parse_batch_file_limit,
    parse_batch_max_bytes,
    parse_chapter_filename,
    process_pipeline,
    resolve_har_file,
    safe_extract,
)
from gosync.manifest import MediaManifest
from gosync.paths import media_download_path
from gosync.progress import ProgressState
from gosync.state import create_or_update_state, load_state, mark_downloaded


class _FakeStreamResponse:
    def __init__(self, chunks, content_length=None, status_ok=True):
        self.chunks = chunks
        self.headers = (
            {} if content_length is None else {"content-length": str(content_length)}
        )
        self._status_ok = status_ok

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("bad status")

    def iter_content(self, chunk_size=8192):
        yield from self.chunks


def _which_ffmpeg_only(binary: str) -> str | None:
    return "/usr/bin/ffmpeg" if binary == "ffmpeg" else None


def _which_ffmpeg_and_exiftool(binary: str) -> str | None:
    return f"/usr/bin/{binary}" if binary in ("ffmpeg", "exiftool") else None


class _FakeSession:
    def __init__(self, response=None, get_error=None):
        self._response = response
        self._get_error = get_error
        self.requested_urls = []

    def get(self, url, **_kwargs):
        self.requested_urls.append(url)
        if self._get_error:
            raise self._get_error
        return self._response


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


def test_size_batches_keep_chaptered_items_single(make_media_item) -> None:
    items = [
        make_media_item("A", "chaptered.mp4", 70, item_count=2),
        make_media_item("B", "small.mp4", 20),
        make_media_item("C", "tiny.jpg", 10),
    ]

    batches = build_size_batches(items, 100)

    assert [[item.filename for item in batch] for batch in batches] == [
        ["chaptered.mp4"],
        ["small.mp4", "tiny.jpg"],
    ]


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


def test_merge_chapter_files_success_orders_chapters_in_concat_list(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name, content in (
        ("GX030320.MP4", "chapter-3"),
        ("GX010320.MP4", "chapter-1"),
        ("GX020320.MP4", "chapter-2"),
    ):
        (output_dir / name).write_text(content, encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)
    captured: dict = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        list_path = Path(cmd[cmd.index("-i") + 1])
        captured["list_lines"] = [
            line
            for line in list_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        Path(cmd[-1]).write_text("chapter-1chapter-2chapter-3", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", _which_ffmpeg_only)
    monkeypatch.setattr(subprocess, "run", fake_run)

    progress = ProgressState(job_id="job-1")
    assert merge_chapter_files(output_dir, item, target_path, progress, "job-1") is True
    assert target_path.read_text(encoding="utf-8") == "chapter-1chapter-2chapter-3"
    assert not (output_dir / "GX010320.MP4").exists()
    assert not (output_dir / "GX020320.MP4").exists()
    assert not (output_dir / "GX030320.MP4").exists()
    originals_dir = output_dir / "original_unmerged_mp4"
    assert (originals_dir / "GX010320.MP4").read_text(encoding="utf-8") == "chapter-1"
    assert (originals_dir / "GX020320.MP4").read_text(encoding="utf-8") == "chapter-2"
    assert (originals_dir / "GX030320.MP4").read_text(encoding="utf-8") == "chapter-3"
    events = progress.snapshot()["events"]
    started_events = [
        event for event in events if event["event"] == "download.chapter.merge_started"
    ]
    assert len(started_events) == 1
    assert progress.snapshot()["state_label"] == "Merging"
    assert captured["list_lines"] == [
        f"file '{(output_dir / 'GX010320.MP4').resolve()}'",
        f"file '{(output_dir / 'GX020320.MP4').resolve()}'",
        f"file '{(output_dir / 'GX030320.MP4').resolve()}'",
    ]
    for flag in ("-map", "-map_metadata", "-movflags", "use_metadata_tags"):
        assert flag in captured["cmd"]


def test_merge_chapter_files_copies_metadata_from_first_chapter(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=2)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name, content in (
        ("GX010320.MP4", "chapter-1"),
        ("GX020320.MP4", "chapter-2"),
    ):
        (output_dir / name).write_text(content, encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)
    exiftool_calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        if cmd[0] == "/usr/bin/exiftool":
            exiftool_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        Path(cmd[-1]).write_text("chapter-1chapter-2", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", _which_ffmpeg_and_exiftool)
    monkeypatch.setattr(subprocess, "run", fake_run)
    progress = ProgressState(job_id="job-1")

    assert merge_chapter_files(output_dir, item, target_path, progress, "job-1") is True

    assert len(exiftool_calls) == 1
    cmd = exiftool_calls[0]
    assert "-TagsFromFile" in cmd
    # Metadata source must be chapter 1 at its pre-merge path (still present
    # at merge time, before originals get moved aside).
    source_arg = cmd[cmd.index("-TagsFromFile") + 1]
    assert source_arg == str((output_dir / "GX010320.MP4").resolve())
    assert "-all:all" in cmd
    assert "--Duration" in cmd
    assert cmd[-1] == str(target_path)
    events = progress.snapshot()["events"]
    assert any(
        event["event"] == "download.chapter.metadata_copied" for event in events
    )


def test_merge_chapter_files_missing_exiftool_still_succeeds_with_warning(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=2)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name, content in (
        ("GX010320.MP4", "chapter-1"),
        ("GX020320.MP4", "chapter-2"),
    ):
        (output_dir / name).write_text(content, encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_text("chapter-1chapter-2", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", _which_ffmpeg_only)
    monkeypatch.setattr(subprocess, "run", fake_run)
    progress = ProgressState(job_id="job-1")

    result = merge_chapter_files(output_dir, item, target_path, progress, "job-1")

    assert result is True
    assert target_path.read_text(encoding="utf-8") == "chapter-1chapter-2"
    events = progress.snapshot()["events"]
    skipped_events = [
        event
        for event in events
        if event["event"] == "download.chapter.metadata_skipped"
    ]
    assert len(skipped_events) == 1
    assert skipped_events[0]["level"] == "warning"


def test_merge_chapter_files_exiftool_failure_still_succeeds_with_warning(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=2)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name, content in (
        ("GX010320.MP4", "chapter-1"),
        ("GX020320.MP4", "chapter-2"),
    ):
        (output_dir / name).write_text(content, encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)

    def fake_run(cmd, **_kwargs):
        if cmd[0] == "/usr/bin/exiftool":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad atom")
        Path(cmd[-1]).write_text("chapter-1chapter-2", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", _which_ffmpeg_and_exiftool)
    monkeypatch.setattr(subprocess, "run", fake_run)
    progress = ProgressState(job_id="job-1")

    result = merge_chapter_files(output_dir, item, target_path, progress, "job-1")

    assert result is True
    assert target_path.read_text(encoding="utf-8") == "chapter-1chapter-2"
    events = progress.snapshot()["events"]
    failed_events = [
        event
        for event in events
        if event["event"] == "download.chapter.metadata_failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["level"] == "warning"
    assert "bad atom" in failed_events[0]["error_details"]


def test_merge_chapter_files_missing_ffmpeg_falls_back(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010320.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020320.MP4").write_text("chapter-2", encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)
    monkeypatch.setattr(shutil, "which", lambda _binary: None)
    progress = ProgressState(job_id="job-1")

    result = merge_chapter_files(output_dir, item, target_path, progress, "job-1")

    assert result is False
    assert not target_path.exists()
    assert (output_dir / "GX010320.MP4").exists()
    assert (output_dir / "GX020320.MP4").exists()
    events = progress.snapshot()["events"]
    assert any(
        event["event"] == "download.chapter.merge_skipped"
        and event["level"] == "warning"
        for event in events
    )


def test_merge_chapter_files_subprocess_failure_preserves_sources(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010320.MP4").write_text("chapter-1", encoding="utf-8")
    (output_dir / "GX020320.MP4").write_text("chapter-2", encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="boom\nsecond line"
        )

    monkeypatch.setattr(shutil, "which", _which_ffmpeg_only)
    monkeypatch.setattr(subprocess, "run", fake_run)
    progress = ProgressState(job_id="job-1")

    result = merge_chapter_files(output_dir, item, target_path, progress, "job-1")

    assert result is False
    assert not target_path.exists()
    assert (output_dir / "GX010320.MP4").read_text(encoding="utf-8") == "chapter-1"
    assert (output_dir / "GX020320.MP4").read_text(encoding="utf-8") == "chapter-2"
    events = progress.snapshot()["events"]
    failed_events = [
        event for event in events if event["event"] == "download.chapter.merge_failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0]["level"] == "error"
    assert "boom" in failed_events[0]["error_details"]


def test_merge_chapter_files_returns_false_when_fewer_than_two_siblings_no_event(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010320.MP4").write_text("chapter-1", encoding="utf-8")
    target_path = media_download_path(output_dir, item.filename)
    progress = ProgressState(job_id="job-1")

    result = merge_chapter_files(output_dir, item, target_path, progress, "job-1")

    assert result is False
    assert progress.snapshot()["events"] == []


def test_organize_extracted_media_merges_chapter_files_end_to_end(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name, content in (
        ("GX010320.MP4", "chapter-1"),
        ("GX020320.MP4", "chapter-2"),
        ("GX030320.MP4", "chapter-3"),
    ):
        (output_dir / name).write_text(content, encoding="utf-8")

    def fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_text("chapter-1chapter-2chapter-3", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(shutil, "which", _which_ffmpeg_only)
    monkeypatch.setattr(subprocess, "run", fake_run)

    completed_keys = organize_extracted_media(output_dir, [item])

    assert item.key in completed_keys
    target_path = media_download_path(output_dir, item.filename)
    assert target_path.read_text(encoding="utf-8") == "chapter-1chapter-2chapter-3"
    assert not (output_dir / "GX020320.MP4").exists()
    assert not (output_dir / "GX030320.MP4").exists()


def test_organize_extracted_media_never_calls_merge_for_non_chapter_items(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "clip.mp4").write_text("media", encoding="utf-8")

    def fail_merge(*_args, **_kwargs):
        raise AssertionError("merge_chapter_files should not be called")

    monkeypatch.setattr("gosync.downloader.merge_chapter_files", fail_merge)

    completed_keys = organize_extracted_media(output_dir, [item])

    assert item.key in completed_keys
    assert media_download_path(output_dir, "clip.mp4").read_text(
        encoding="utf-8"
    ) == "media"


def test_process_pipeline_merges_chapter_batch_and_marks_downloaded(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010320.MP4", 30, item_count=3)
    state_file = tmp_path / "state.json"
    output_dir = tmp_path / "downloads"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[item],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )

    def fake_download_batch(
        _session,
        _batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
        **_kwargs,
    ):
        with zipfile.ZipFile(temp_zip, "w") as zip_ref:
            zip_ref.writestr("GX010320.MP4", "chapter-1")
            zip_ref.writestr("GX020320.MP4", "chapter-2")
            zip_ref.writestr("GX030320.MP4", "chapter-3")

    def fake_merge(
        output_dir_arg, merge_item, target_path, _progress=None, _job_id=None
    ):
        chapters = find_chapter_source_files(output_dir_arg, merge_item)
        merged = "".join(path.read_text(encoding="utf-8") for path in chapters)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(merged, encoding="utf-8")
        for path in chapters:
            path.unlink()
        return True

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)
    monkeypatch.setattr("gosync.downloader.merge_chapter_files", fake_merge)

    process_pipeline(
        media_items=[item],
        data_dir=tmp_path,
        output_dir=output_dir,
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
    )

    state = load_state(state_file)
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED
    merged_path = media_download_path(output_dir, item.filename)
    assert merged_path.read_text(encoding="utf-8") == "chapter-1chapter-2chapter-3"
    assert not (output_dir / "GX020320.MP4").exists()
    assert not (output_dir / "GX030320.MP4").exists()
    # The merged file (27 bytes) never matches item.file_size (30, the API's
    # per-chapter sum) exactly -- process_pipeline must persist the real
    # on-disk size so later resume-scans compare against reality.
    assert state["media"][item.key]["file_size"] == merged_path.stat().st_size
    assert state["media"][item.key]["file_size"] != item.file_size


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_merge_chapter_files_real_ffmpeg_smoke(tmp_path: Path, make_media_item) -> None:
    """Wiring/CLI-argument smoke test against the real ffmpeg binary.

    Chapter ORDER correctness is proven by the mocked tests above via the
    generated concat list; real per-chapter durations are order-invariant so
    proving playback order from real ffmpeg output would need brittle
    pixel-level probing that isn't worth the maintenance cost here.
    """
    item = make_media_item("A", "GX010500.MP4", None, item_count=2)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name in ("GX010500.MP4", "GX020500.MP4"):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=0.1",
                "-pix_fmt",
                "yuv420p",
                str(output_dir / name),
            ],
            capture_output=True,
            check=True,
        )
    target_path = media_download_path(output_dir, item.filename)

    assert merge_chapter_files(output_dir, item, target_path) is True
    assert target_path.exists()
    assert target_path.stat().st_size > 0


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


def test_process_pipeline_passes_data_dir_temp_zip_to_download_batch(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[item],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )
    fake_session = object()
    download_calls = []

    def fake_download_batch(
        session,
        batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
    ):
        download_calls.append((session, batch, temp_zip))
        with zipfile.ZipFile(temp_zip, "w"):
            pass

    monkeypatch.setattr("gosync.downloader.create_session", lambda: fake_session)
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)
    monkeypatch.setattr("gosync.downloader.organize_extracted_media", lambda *_: None)

    process_pipeline(
        media_items=[item],
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
    )

    assert download_calls == [(fake_session, ["A"], tmp_path / DEFAULT_TEMP_ZIP)]


def test_process_pipeline_moves_case_variant_file_and_marks_downloaded(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "GX010002.MP4", 10)
    state_file = tmp_path / "state.json"
    output_dir = tmp_path / "downloads"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[item],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )

    def fake_download_batch(
        _session,
        _batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
        **_kwargs,
    ):
        with zipfile.ZipFile(temp_zip, "w") as zip_ref:
            zip_ref.writestr("gx010002.mp4", "media")
            zip_ref.writestr("GX020002.mp4", "chapter")

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)

    process_pipeline(
        media_items=[item],
        data_dir=tmp_path,
        output_dir=output_dir,
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
    )

    state = load_state(state_file)
    assert state["media"][item.key]["download_status"] == STATUS_DOWNLOADED
    assert not (output_dir / "gx010002.mp4").exists()
    assert media_download_path(output_dir, item.filename).read_text(
        encoding="utf-8"
    ) == "media"
    assert media_download_path(output_dir, "GX020002.mp4").read_text(
        encoding="utf-8"
    ) == "chapter"


def test_process_pipeline_does_not_mark_missing_extracted_file_downloaded(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "missing.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[item],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )

    def fake_download_batch(
        _session,
        _batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
        **_kwargs,
    ):
        with zipfile.ZipFile(temp_zip, "w"):
            pass

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)

    process_pipeline(
        media_items=[item],
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
    )

    state = load_state(state_file)
    assert state["media"][item.key]["download_status"] == STATUS_FAILED


def test_process_pipeline_counts_progress_against_full_manifest(
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
        progress_media_items=[already_done, selected],
        job_id="job-1",
    )

    snapshot = progress.snapshot()
    assert snapshot["total_ids"] == 2
    assert snapshot["completed_ids"] == 2
    assert snapshot["pending_ids"] == 0


def test_single_file_completed_event_is_emitted_after_state_update(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[item],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )
    progress = ProgressState(job_id="job-1")

    def fake_download_batch(
        _session,
        _batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
        **_kwargs,
    ):
        with zipfile.ZipFile(temp_zip, "w"):
            pass

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)
    monkeypatch.setattr("gosync.downloader.organize_extracted_media", lambda *_: None)

    process_pipeline(
        media_items=[item],
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
        progress=progress,
        job_id="job-1",
    )

    events = progress.snapshot()["events"]
    completed_indexes = [
        index
        for index, event in enumerate(events)
        if event["event"] == "download.file.completed"
    ]
    batch_completed_index = next(
        index
        for index, event in enumerate(events)
        if event["event"] == "download.batch.completed"
    )

    assert completed_indexes == [batch_completed_index + 1]


def test_single_file_completed_event_is_not_emitted_when_extraction_fails(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=[item],
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )
    progress = ProgressState(job_id="job-1")

    def fake_download_batch(
        _session,
        _batch,
        temp_zip,
        _headers,
        _progress=None,
        _job_id=None,
        **_kwargs,
    ):
        with zipfile.ZipFile(temp_zip, "w"):
            pass

    monkeypatch.setattr("gosync.downloader.create_session", lambda: object())
    monkeypatch.setattr("gosync.downloader.download_batch", fake_download_batch)
    monkeypatch.setattr(
        "gosync.downloader.organize_extracted_media",
        lambda *_: (_ for _ in ()).throw(RuntimeError("extract failed")),
    )

    process_pipeline(
        media_items=[item],
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
        progress=progress,
        job_id="job-1",
    )

    assert not any(
        event["event"] == "download.file.completed"
        for event in progress.snapshot()["events"]
    )


def test_process_pipeline_keeps_activity_message_to_batch_summary(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    items = [
        make_media_item("A", "first.mp4", 10),
        make_media_item("B", "second.mp4", 10),
    ]
    state_file = tmp_path / "state.json"
    create_or_update_state(
        state_file,
        MediaManifest(
            media=items,
            duplicates=[],
            matching_entries=1,
            media_responses=[],
        ),
    )
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
        media_items=items,
        data_dir=tmp_path,
        output_dir=tmp_path / "downloads",
        state_file=state_file,
        headers={},
        batch_max_bytes="auto",
        progress=progress,
        job_id="job-1",
    )

    snapshot = progress.snapshot()
    assert any(
        event["event"] == "download.batch.started"
        and event["batch_total"] == 2
        and event["files_in_batch"] == 1
        and event["files"][0]["file_name"] == "first.mp4"
        and event["files"][0]["file_size_human"] == "10.00 B"
        and event["detail_lines"] == ["first.mp4 · 10.00 B"]
        for event in snapshot["events"]
    )
    assert sum(
        1
        for event in snapshot["events"]
        if event["event"] == "download.batch.started"
        and event["batch_index"] == 1
    ) == 1
    assert any(
        event["level"] == "active"
        and event["title"] == "Extracting batch 1 of 2"
        and event["message"] == "1 file ready to unpack."
        for event in snapshot["events"]
    )
    assert "Files in batch" not in snapshot["message"]
    assert "first.mp4" not in snapshot["message"]
    assert snapshot["state_label"] == "Completed"
    assert snapshot["message"] == "Batch 2 of 2 complete: 1 file downloaded."


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


def test_download_batch_single_file_writes_zip_and_emits_file_progress(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "clip.mp4", 10)
    content = b"zip-bytes-payload"
    response = _FakeStreamResponse(
        [content[:5], content[5:]], content_length=len(content)
    )
    session = _FakeSession(response=response)
    temp_zip = tmp_path / "batch.zip"
    progress = ProgressState(job_id="job-1")

    download_batch(
        session,
        ["A"],
        temp_zip,
        {"Cookie": "session=abc"},
        progress,
        "job-1",
        batch_items=[item],
        batch_index=1,
        batch_total=1,
    )

    assert temp_zip.read_bytes() == content
    assert session.requested_urls == [f"{ZIP_URL_PREFIX}?ids=A"]

    snapshot = progress.snapshot()
    assert snapshot["state_label"] == "Downloading"
    assert snapshot["current_download_bytes"] == len(content)
    events = snapshot["events"]
    assert any(event["event"] == "download.file.started" for event in events)
    progress_events = [
        event for event in events if event["event"] == "download.file.progress"
    ]
    assert progress_events
    assert progress_events[-1]["progress_percent"] == 100


def test_download_batch_multi_file_batch_skips_per_file_events(
    tmp_path: Path,
    make_media_item,
) -> None:
    items = [make_media_item("A", "a.mp4", 5), make_media_item("B", "b.mp4", 5)]
    content = b"multi-file-zip-content"
    response = _FakeStreamResponse([content], content_length=len(content))
    session = _FakeSession(response=response)
    temp_zip = tmp_path / "batch.zip"
    progress = ProgressState(job_id="job-1")

    download_batch(
        session,
        ["A", "B"],
        temp_zip,
        {},
        progress,
        "job-1",
        batch_items=items,
        batch_index=1,
        batch_total=2,
    )

    assert temp_zip.read_bytes() == content
    events = progress.snapshot()["events"]
    assert not any(event["event"] == "download.file.started" for event in events)
    assert not any(event["event"] == "download.file.progress" for event in events)


def test_download_batch_handles_missing_content_length(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "clip.mp4", None)
    response = _FakeStreamResponse([b"chunk-a", b"chunk-b"])
    session = _FakeSession(response=response)
    temp_zip = tmp_path / "batch.zip"
    progress = ProgressState(job_id="job-1")

    download_batch(
        session,
        ["A"],
        temp_zip,
        {},
        progress,
        "job-1",
        batch_items=[item],
        batch_index=1,
        batch_total=1,
    )

    progress_events = [
        event
        for event in progress.snapshot()["events"]
        if event["event"] == "download.file.progress"
    ]
    # In-progress and final "download.file.progress" events for the same
    # file/batch collapse into a single stored event (see
    # ProgressState._append_structured_event), so only the final, post-loop
    # values are observable here.
    assert len(progress_events) == 1
    assert progress_events[0]["progress_percent"] == 0


def test_download_batch_raises_cancelled_when_stop_requested(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "clip.mp4", 5)
    response = _FakeStreamResponse([b"abc", b"def"], content_length=6)
    session = _FakeSession(response=response)
    temp_zip = tmp_path / "batch.zip"
    progress = ProgressState(job_id="job-1", stop_requested=True)

    with pytest.raises(DownloadCancelled):
        download_batch(
            session,
            ["A"],
            temp_zip,
            {},
            progress,
            "job-1",
            batch_items=[item],
            batch_index=1,
            batch_total=1,
        )


def test_download_batch_wraps_retryable_network_errors(tmp_path: Path) -> None:
    session = _FakeSession(get_error=RequestsConnectionError("boom"))
    temp_zip = tmp_path / "batch.zip"

    with pytest.raises(RuntimeError, match="Retryable download error"):
        download_batch(session, ["A"], temp_zip, {})


def test_extract_browser_headers_returns_defaults_on_invalid_json(
    tmp_path: Path,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("not json", encoding="utf-8")
    progress = ProgressState(job_id="job-1")

    headers = extract_browser_headers(har_path, progress, "job-1")

    assert headers == DEFAULT_HEADERS
    events = progress.snapshot()["events"]
    assert any(event["event"] == "error.validation" for event in events)


def test_extract_browser_headers_warns_when_no_gopro_request_found(
    tmp_path: Path,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "url": "https://example.com/other",
                                "headers": [],
                            }
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    progress = ProgressState(job_id="job-1")

    headers = extract_browser_headers(har_path, progress, "job-1")

    assert headers == DEFAULT_HEADERS
    events = progress.snapshot()["events"]
    assert any(
        event["event"] == "error.auth"
        and "No GoPro browser request" in event["message"]
        for event in events
    )


def test_extract_browser_headers_prefers_zip_download_request(tmp_path: Path) -> None:
    har_path = tmp_path / "gopro.com.har"
    entries = [
        {
            "request": {
                "url": "https://api.gopro.com/media/search?page=1",
                "headers": [{"name": "Cookie", "value": "first-session"}],
            }
        },
        {
            "request": {
                "url": f"{ZIP_URL_PREFIX}?ids=A,B",
                "headers": [{"name": "Cookie", "value": "zip-session"}],
            }
        },
    ]
    har_path.write_text(
        json.dumps({"log": {"entries": entries}}), encoding="utf-8"
    )

    headers = extract_browser_headers(har_path)

    assert headers["Cookie"] == "zip-session"


def test_extract_browser_headers_copies_auth_headers_and_emits_reused_event(
    tmp_path: Path,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    entries = [
        {
            "request": {
                "url": "https://api.gopro.com/media/search?page=1",
                "headers": [
                    {"name": "Cookie", "value": "session=abc"},
                    {"name": "Authorization", "value": "Bearer xyz"},
                    {"name": "content-length", "value": "0"},
                    {"name": "", "value": "ignored"},
                ],
            }
        },
    ]
    har_path.write_text(
        json.dumps({"log": {"entries": entries}}), encoding="utf-8"
    )
    progress = ProgressState(job_id="job-1")

    headers = extract_browser_headers(har_path, progress, "job-1")

    assert headers["Cookie"] == "session=abc"
    assert headers["Authorization"] == "Bearer xyz"
    assert "content-length" not in headers
    assert headers["User-Agent"] == DEFAULT_HEADERS["User-Agent"]
    reused_events = [
        event
        for event in progress.snapshot()["events"]
        if event["event"] == "auth.session.reused"
    ]
    assert len(reused_events) == 1
    assert reused_events[0]["auth_mode"] == "authorization+cookie"


def test_extract_browser_headers_warns_when_missing_cookie_and_authorization(
    tmp_path: Path,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    entries = [
        {
            "request": {
                "url": "https://api.gopro.com/media/search?page=1",
                "headers": [{"name": "Accept", "value": "application/json"}],
            }
        },
    ]
    har_path.write_text(
        json.dumps({"log": {"entries": entries}}), encoding="utf-8"
    )
    progress = ProgressState(job_id="job-1")

    headers = extract_browser_headers(har_path, progress, "job-1")

    assert headers["Accept"] == "application/json"
    events = progress.snapshot()["events"]
    assert any(
        event["event"] == "error.auth"
        and "Cookie or Authorization" in event["message"]
        for event in events
    )
