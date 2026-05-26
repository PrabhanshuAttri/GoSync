from pathlib import Path
from types import SimpleNamespace

from gosync import runtime
from gosync.constants import STATUS_DOWNLOADED
from gosync.manifest import MediaManifest
from gosync.progress import ProgressState


def test_run_download_job_preserves_startup_counts_while_scanning(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    items = [
        make_media_item("A", "done.mp4", 10),
        make_media_item("B", "pending.mp4", 10),
        make_media_item("C", "other.jpg", 10),
    ]
    state = {
        "media": {
            items[0].key: {
                "key": items[0].key,
                "download_status": STATUS_DOWNLOADED,
            },
            items[1].key: {"key": items[1].key, "download_status": "pending"},
            items[2].key: {"key": items[2].key, "download_status": "pending"},
        }
    }
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
    )
    progress = ProgressState(
        job_id="job-1",
        total_ids=3,
        completed_ids=1,
        pending_ids=2,
    )

    def fake_prepare(paths):
        snapshot = progress.snapshot()
        assert snapshot["total_ids"] == 3
        assert snapshot["completed_ids"] == 1
        assert snapshot["pending_ids"] == 2
        return runtime.PreparedManifestState(
            paths=paths,
            manifest=MediaManifest(
                media=items,
                duplicates=[],
                matching_entries=1,
                media_responses=[],
            ),
            state=state,
            sync_changes=[],
        )

    monkeypatch.setattr(runtime, "prepare_paths_manifest_state", fake_prepare)
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_args: {})
    monkeypatch.setattr(runtime, "process_pipeline", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "load_state", lambda _path: state)
    monkeypatch.setattr(
        runtime,
        "write_run_report",
        lambda *_args: tmp_path / "report.json",
    )

    runtime.run_download_job(args, progress, "gopro.com.har", job_id="job-1")

    snapshot = progress.snapshot()
    assert snapshot["total_ids"] == 3
    assert snapshot["completed_ids"] == 1
    assert snapshot["events"][-1]["title"] == "Run complete"
    assert "1 of 3 media files downloaded" in snapshot["events"][-1]["details"]
