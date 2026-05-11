from gosync.progress import ProgressState


def test_progress_ignores_stale_job_updates_and_notifications() -> None:
    progress = ProgressState(job_id="current")

    assert not progress.update(job_id_guard="stale", status="failed")
    assert not progress.log("stale message", job_id_guard="stale")
    assert not progress.notify("error", "Stale", "ignored", job_id_guard="stale")

    snapshot = progress.snapshot()
    assert snapshot["status"] == "idle"
    assert snapshot["message"] == "Ready"
    assert snapshot["notifications"] == []

    assert progress.update(job_id_guard="current", status="running")
    assert progress.log("current message", job_id_guard="current")

    snapshot = progress.snapshot()
    assert snapshot["status"] == "running"
    assert snapshot["message"] == "current message"
