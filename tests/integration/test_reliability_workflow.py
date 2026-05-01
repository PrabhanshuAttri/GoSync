import json
from pathlib import Path

from gosync.paths import media_download_path, sidecar_output_path
from gosync.report import build_run_summary, write_run_report
from gosync.runtime import prepare_manifest_state
from gosync.sidecar import run_sidecar_job
from gosync.state import load_state, sync_state_with_downloads


def test_prepare_manifest_state_writes_manifest_media_dump_and_state(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_sample_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    manifest_file = tmp_path / "manifest.json"
    media_dump_file = tmp_path / "media_search.json"
    downloads = tmp_path / "downloads"

    manifest, state, changes = prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        manifest_file,
        media_dump_file,
    )

    assert [item.filename for item in manifest.media] == [
        "GX010001.MP4",
        "GX010002.JPG",
        "unnamed_1.MP4",
    ]
    assert len(state["media"]) == 3
    assert changes == []

    manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_payload["media_count"] == 3
    assert manifest_payload["duplicate_count"] == 1
    assert manifest_payload["media"][2]["filename"] == "unnamed_1.MP4"

    dump = json.loads(media_dump_file.read_text(encoding="utf-8"))
    assert dump["matching_responses"] == 1
    assert dump["media_count"] == 4
    assert dump["media"][0]["filename"] == "GX010001.MP4"
    assert dump["media"][2]["filename"] == "unnamed_1.MP4"
    assert dump["media"][2]["id"] == "UNNAMEDMEDIA1"
    assert state["media"]["UNNAMEDMEDIA1_unnamed_1.MP4"]["id"] == "UNNAMEDMEDIA1"


def test_resume_sync_detects_files_added_and_removed_after_prepare(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_sample_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    downloads = tmp_path / "downloads"
    prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        tmp_path / "manifest.json",
        tmp_path / "media_search.json",
    )

    jpg_path = media_download_path(downloads, "GX010002.JPG")
    jpg_path.parent.mkdir(parents=True)
    jpg_path.write_text("done", encoding="utf-8")
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert {
        (change["filename"], change["status"]) for change in changes
    } == {("GX010002.JPG", "found")}
    assert state["media"]["NOPQRSTUVWXYZ_GX010002.JPG"]["download_status"] == (
        "downloaded"
    )

    jpg_path.unlink()
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert {
        (change["filename"], change["status"]) for change in changes
    } == {("GX010002.JPG", "missing")}
    assert state["media"]["NOPQRSTUVWXYZ_GX010002.JPG"]["download_status"] == (
        "pending"
    )


def test_sidecar_job_uses_manifest_items_and_updates_json_state(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_sample_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    downloads = tmp_path / "downloads"
    manifest, _state, _changes = prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        tmp_path / "manifest.json",
        tmp_path / "media_search.json",
    )

    run_sidecar_job(
        har_path,
        downloads,
        media_items=manifest.media,
        state_file=state_file,
    )
    state = load_state(state_file)

    assert sidecar_output_path(
        downloads,
        "GX010001.MP4",
        "GX010001.MP4.xmp",
    ).exists()
    assert sidecar_output_path(
        downloads,
        "GX010002.JPG",
        "GX010002.JPG.xmp",
    ).exists()
    assert sidecar_output_path(
        downloads,
        "unnamed_1.MP4",
        "unnamed_1.MP4.xmp",
    ).exists()
    assert {
        record["sidecar_status"] for record in state["media"].values()
    } == {"complete"}


def test_run_summary_and_report_reflect_prepared_manifest_duplicates(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_sample_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    downloads = tmp_path / "downloads"
    manifest, state, _changes = prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        tmp_path / "manifest.json",
        tmp_path / "media_search.json",
    )

    summary = build_run_summary(
        state,
        manifest,
        "complete",
        [{"id": "1", "filename": "a.mp4", "status": "found"}],
        tmp_path / "reports" / "run.json",
    )
    report_path = write_run_report(
        tmp_path,
        state,
        manifest,
        "complete",
        [{"id": "1", "filename": "a.mp4", "status": "found"}],
    )

    assert "Run summary" in summary
    assert "Total media: 3" in summary
    assert "Pending: 3" in summary
    assert "Skipped duplicates: 1" in summary
    assert "Resume sync changes: 1" in summary
    assert "Report:" in summary

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["total_media"] == 3
    assert report["pending_count"] == 3
    assert report["duplicates"] == [
        {
            "id": "ABCDEFGHIJKLM",
            "filename": "GX010001.MP4",
            "key": "ABCDEFGHIJKLM_GX010001.MP4",
        }
    ]

