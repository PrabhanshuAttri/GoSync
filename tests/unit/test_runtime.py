from pathlib import Path
from types import SimpleNamespace

from gosync import runtime
from gosync.constants import STATUS_DOWNLOADED
from gosync.downloader import DownloadCancelled
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


def test_run_download_job_fetches_telemetry_and_sidecars_before_downloading_media(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    items = [make_media_item("A", "clip.mp4", 10)]
    state = {
        "media": {items[0].key: {"key": items[0].key, "download_status": "pending"}}
    }
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
        download_telemetry=True,
        create_xmp_sidecars=True,
    )
    progress = ProgressState(job_id="job-1")
    call_order: list[str] = []

    def fake_prepare(paths):
        return runtime.PreparedManifestState(
            paths=paths,
            manifest=MediaManifest(
                media=items, duplicates=[], matching_entries=1, media_responses=[]
            ),
            state=state,
            sync_changes=[],
        )

    monkeypatch.setattr(runtime, "prepare_paths_manifest_state", fake_prepare)
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(
        runtime,
        "run_telemetry_job",
        lambda *_a, **_k: (call_order.append("telemetry"), (1, 0))[1],
    )
    monkeypatch.setattr(
        runtime, "run_sidecar_job", lambda *_a, **_k: call_order.append("sidecar")
    )
    monkeypatch.setattr(
        runtime, "process_pipeline", lambda **_k: call_order.append("download")
    )
    monkeypatch.setattr(runtime, "load_state", lambda _path: state)
    monkeypatch.setattr(
        runtime, "write_run_report", lambda *_a, **_k: tmp_path / "report.json"
    )

    runtime.run_download_job(args, progress, "gopro.com.har", job_id="job-1")

    assert call_order == ["telemetry", "sidecar", "download"]


def test_run_once_completes_and_writes_report(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
    )
    paths = runtime.get_runtime_paths(args)
    items = [make_media_item("A", "clip.mp4", 10)]
    manifest = MediaManifest(
        media=items,
        duplicates=[],
        matching_entries=1,
        media_responses=[],
    )
    prepared = runtime.PreparedManifestState(
        paths=paths,
        manifest=manifest,
        state={"media": {}},
        sync_changes=[],
    )
    final_state = {
        "media": {
            items[0].key: {
                "key": items[0].key,
                "download_status": STATUS_DOWNLOADED,
            }
        }
    }
    report_path = tmp_path / "reports" / "report.json"
    process_pipeline_calls = []

    monkeypatch.setattr(
        runtime, "prepare_runtime_manifest_state", lambda *_a, **_k: prepared
    )
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(runtime, "run_sidecar_job", lambda *_a, **_k: None)
    monkeypatch.setattr(
        runtime,
        "process_pipeline",
        lambda **kwargs: process_pipeline_calls.append(kwargs),
    )
    monkeypatch.setattr(runtime, "load_state", lambda _path: final_state)
    monkeypatch.setattr(runtime, "write_run_report", lambda *_a, **_k: report_path)

    result = runtime.run_once(args)

    assert result == 0
    assert len(process_pipeline_calls) == 1
    assert process_pipeline_calls[0]["media_items"] == items


def test_run_once_fetches_telemetry_and_sidecars_before_downloading_media(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
        download_telemetry=True,
        create_xmp_sidecars=True,
    )
    paths = runtime.get_runtime_paths(args)
    items = [make_media_item("A", "clip.mp4", 10)]
    manifest = MediaManifest(
        media=items, duplicates=[], matching_entries=1, media_responses=[]
    )
    prepared = runtime.PreparedManifestState(
        paths=paths, manifest=manifest, state={"media": {}}, sync_changes=[]
    )
    call_order: list[str] = []

    monkeypatch.setattr(
        runtime, "prepare_runtime_manifest_state", lambda *_a, **_k: prepared
    )
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(
        runtime,
        "run_telemetry_job",
        lambda *_a, **_k: call_order.append("telemetry"),
    )
    monkeypatch.setattr(
        runtime, "run_sidecar_job", lambda *_a, **_k: call_order.append("sidecar")
    )
    monkeypatch.setattr(
        runtime, "process_pipeline", lambda **_k: call_order.append("download")
    )
    monkeypatch.setattr(runtime, "load_state", lambda _path: {"media": {}})
    monkeypatch.setattr(
        runtime, "write_run_report", lambda *_a, **_k: tmp_path / "report.json"
    )

    runtime.run_once(args)

    assert call_order == ["telemetry", "sidecar", "download"]


def test_run_once_continues_when_sidecar_job_raises(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
    )
    paths = runtime.get_runtime_paths(args)
    items = [make_media_item("A", "clip.mp4", 10)]
    manifest = MediaManifest(
        media=items,
        duplicates=[],
        matching_entries=1,
        media_responses=[],
    )
    prepared = runtime.PreparedManifestState(
        paths=paths,
        manifest=manifest,
        state={"media": {}},
        sync_changes=[],
    )

    def failing_sidecar_job(*_args, **_kwargs):
        raise RuntimeError("sidecar boom")

    monkeypatch.setattr(
        runtime, "prepare_runtime_manifest_state", lambda *_a, **_k: prepared
    )
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(runtime, "run_sidecar_job", failing_sidecar_job)
    monkeypatch.setattr(runtime, "process_pipeline", lambda **_kwargs: None)
    monkeypatch.setattr(runtime, "load_state", lambda _path: {"media": {}})
    monkeypatch.setattr(
        runtime, "write_run_report", lambda *_a, **_k: tmp_path / "report.json"
    )

    result = runtime.run_once(args)

    assert result == 0


def test_run_download_job_handles_cancellation_and_writes_stopped_report(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    items = [make_media_item("A", "clip.mp4", 10)]
    state = {
        "media": {items[0].key: {"key": items[0].key, "download_status": "pending"}}
    }
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
    )
    progress = ProgressState(job_id="job-1")

    def fake_prepare(paths):
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

    def raise_cancelled(**_kwargs):
        raise DownloadCancelled("stop requested")

    monkeypatch.setattr(runtime, "prepare_paths_manifest_state", fake_prepare)
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_args, **_kw: {})
    monkeypatch.setattr(runtime, "process_pipeline", raise_cancelled)
    monkeypatch.setattr(runtime, "load_state", lambda _path: state)
    report_path = tmp_path / "reports" / "stopped-report.json"
    monkeypatch.setattr(runtime, "write_run_report", lambda *_a, **_k: report_path)

    runtime.run_download_job(args, progress, "gopro.com.har", job_id="job-1")

    snapshot = progress.snapshot()
    assert snapshot["status"] == "stopped"
    assert snapshot["state_label"] == "Stopped"
    assert snapshot["report_path"] == str(report_path)
    assert snapshot["events"][-1]["event"] == "run.stopped"
    assert snapshot["events"][-1]["level"] == "warning"


def test_run_download_job_handles_unhandled_exception_marks_failed(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    har_path.write_text("{}", encoding="utf-8")
    items = [make_media_item("A", "clip.mp4", 10)]
    args = SimpleNamespace(
        data_dir=str(tmp_path),
        har_file=None,
        output_folder="downloads",
        state_file="state.json",
        batch_max_bytes="auto",
    )
    progress = ProgressState(job_id="job-1")

    def fake_prepare(paths):
        return runtime.PreparedManifestState(
            paths=paths,
            manifest=MediaManifest(
                media=items,
                duplicates=[],
                matching_entries=1,
                media_responses=[],
            ),
            state={"media": {}},
            sync_changes=[],
        )

    def raise_unexpected(**_kwargs):
        raise ValueError("unexpected boom")

    monkeypatch.setattr(runtime, "prepare_paths_manifest_state", fake_prepare)
    monkeypatch.setattr(runtime, "extract_browser_headers", lambda *_args, **_kw: {})
    monkeypatch.setattr(runtime, "process_pipeline", raise_unexpected)

    runtime.run_download_job(args, progress, "gopro.com.har", job_id="job-1")

    snapshot = progress.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["state_label"] == "Failed"
    last_event = snapshot["events"][-1]
    assert last_event["event"] == "error.unhandled"
    assert last_event["level"] == "error"
    assert "unexpected boom" in last_event["error_message"]


def test_run_metadata_update_job_forces_telemetry_then_generates_sidecars(
    tmp_path: Path,
    make_media_item,
    monkeypatch,
) -> None:
    items = [make_media_item("A", "clip.mp4", 10)]
    call_order: list[str] = []
    telemetry_calls = []

    def fake_telemetry(*_a, **kwargs):
        call_order.append("telemetry")
        telemetry_calls.append(kwargs)
        return (1, 0)

    def fake_sidecar(*_a, **_k):
        call_order.append("sidecar")

    monkeypatch.setattr(runtime, "run_telemetry_job", fake_telemetry)
    monkeypatch.setattr(runtime, "run_sidecar_job", fake_sidecar)

    runtime.run_metadata_update_job(
        {"Authorization": "Bearer abc"},
        items,
        tmp_path / "downloads",
        tmp_path / "gopro.com.har",
        tmp_path / "state.json",
        job_id="job-1",
    )

    assert call_order == ["telemetry", "sidecar"]
    assert telemetry_calls[0]["force"] is True
