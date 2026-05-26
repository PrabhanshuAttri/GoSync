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
    event = snapshot["events"][0]
    assert event["event"] == "download.phase.started"
    assert event["run_id"] == "current"
    assert event["level"] == "active"
    assert event["severity"] == "ACTIVE"
    assert event["title"] == "current message"
    assert event["message"] == "current message"


def test_progress_events_are_structured_json_objects() -> None:
    progress = ProgressState(job_id="current")

    assert progress.log_event(
        "Batch downloaded:\n  - clip.mp4",
        state_label="Completed",
        job_id_guard="current",
    )

    event = progress.snapshot()["events"][0]
    assert {"timestamp", "level", "event", "message", "run_id", "phase"}.issubset(
        event
    )
    assert event["level"] == "success"
    assert event["severity"] == "SUCCESS"
    assert event["event"] == "app.ready"
    assert event["title"] == "Completed"
    assert event["message"] == "Batch downloaded:"
    assert event["details"] == "  - clip.mp4"


def test_progress_accepts_structured_events() -> None:
    progress = ProgressState(job_id="current")

    assert progress.log_structured_event(
        "Run stopped",
        level="warning",
        details="38 of 1528 media files downloaded\n1490 pending",
        job_id_guard="current",
    )

    event = progress.snapshot()["events"][0]
    assert event["level"] == "warning"
    assert event["title"] == "Run stopped"
    assert event["message"] == ""
    assert event["details"] == "38 of 1528 media files downloaded\n1490 pending"
