import shutil
import subprocess

import pytest

from gosync.downloader import merge_chapter_files
from gosync.paths import media_download_path
from gosync.verify import verify_one

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _make_synthetic_chapter(path, duration_seconds: float) -> None:
    """A tiny, real, decodable MP4 -- deliberately not GoPro footage (no
    gpmd/tmcd tracks), so the payload/packet checks that depend on those
    tracks skip gracefully rather than needing a GoPro-shaped fixture."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration_seconds}:size=64x64:rate=5",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_verify_one_passes_against_a_real_merged_chapter_pair(
    tmp_path,
    make_media_item,
) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    _make_synthetic_chapter(output_dir / "GX010401.MP4", 1.0)
    _make_synthetic_chapter(output_dir / "GX020401.MP4", 1.0)

    item = make_media_item("A", "GX010401.MP4", None, item_count=2)
    target_path = media_download_path(output_dir, item.filename)

    assert merge_chapter_files(output_dir, item, target_path) is True
    assert target_path.exists()

    result = verify_one("GX010401.MP4", output_dir)

    assert result["status"] is True
    assert result["chapter_count"] == 2
    check_labels = {label for label, _status, _detail in result["checks"]}
    assert "stream layout" in check_labels
    assert "duration" in check_labels
    assert "packet counts" in check_labels
    assert "playability spot-check" in check_labels
    # Synthetic fixtures have no GoPro gpmd telemetry track, so that check
    # must skip rather than fail or silently not run at all.
    gpmd_checks = [c for c in result["checks"] if c[0] == "gpmd telemetry payload"]
    assert gpmd_checks and gpmd_checks[0][1] == "SKIP"
    for label, status, detail in result["checks"]:
        assert status in ("PASS", "SKIP"), f"{label} unexpectedly {status}: {detail}"


def test_verify_one_skips_when_chapters_not_on_disk(tmp_path) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()

    result = verify_one("GX010402.MP4", output_dir)

    assert result["status"] is None
    assert result["checks"] == []
    assert "chapter" in result["skip_reason"]
