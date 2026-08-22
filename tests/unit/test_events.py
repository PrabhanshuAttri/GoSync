from gosync.events import ProgressEventThrottle, format_cli_message, log_event


def test_log_event_adds_required_shape_and_redacts_sensitive_fields() -> None:
    event = log_event(
        "auth.session.reused",
        "Browser session reused",
        phase="auth",
        run_id="run-1",
        auth_mode="cookie",
        cookie="secret-cookie",
        nested={"access_token": "secret-token"},
    )

    assert event["timestamp"].endswith("Z")
    assert event["level"] == "INFO"
    assert event["event"] == "auth.session.reused"
    assert event["message"] == "Browser session reused"
    assert event["run_id"] == "run-1"
    assert event["phase"] == "auth"
    assert event["cookie"] == "[REDACTED]"
    assert event["nested"]["access_token"] == "[REDACTED]"


def test_progress_event_throttle_limits_frequent_updates() -> None:
    throttle = ProgressEventThrottle(interval_seconds=0.5, percent_step=1.0)

    assert throttle.should_emit(1, 100, now=1.0)
    throttle.mark_emitted(1, 100, now=1.0)
    assert not throttle.should_emit(1, 100, now=1.1)
    assert throttle.should_emit(2, 100, now=1.1)
    assert throttle.should_emit(1, 100, now=1.6)
    assert throttle.should_emit(100, 100, now=1.1)


def test_unknown_event_names_are_marked_unstable() -> None:
    event = log_event("custom.event", "Custom")

    assert event["unstable_event_name"] is True


def test_batch_events_include_ui_metadata_and_file_sizes() -> None:
    event = log_event(
        "download.batch.started",
        "Batch started",
        phase="download",
        batch_index=2,
        batch_total=9,
        files_in_batch=2,
        files=[
            {
                "file_name": "GX010031.MP4",
                "file_id": "GOPR12345678",
                "file_size_bytes": 851863142,
                "file_size_human": "812.40 MiB",
            },
            {
                "file_name": "GX010032.JPG",
                "file_id": "GOPR12345679",
                "file_size_bytes": 8713667,
                "file_size_human": "8.31 MiB",
            },
        ],
    )

    assert event["group"] == "download"
    assert event["group_label"] == "Download"
    assert event["summary"] == "Batch 2/9 started"
    assert "2 files" in event["meta"]
    assert "820.71 MiB" in event["meta"]
    assert event["detail_lines"] == [
        "GX010031.MP4 · 812.40 MiB",
        "GX010032.JPG · 8.31 MiB",
    ]
    assert event["files"][0]["file_name"] == "GX010031.MP4"


def test_verbose_cli_batch_started_lists_files_and_sizes() -> None:
    message = format_cli_message(
        "download.batch.started",
        "Batch started",
        {
            "phase": "download",
            "batch_index": 2,
            "batch_total": 9,
            "files_in_batch": 1,
            "files": [
                {
                    "file_name": "GX010031.MP4",
                    "file_id": "GOPR12345678",
                    "file_size_bytes": 851863142,
                    "file_size_human": "812.40 MiB",
                }
            ],
        },
    )

    assert message.splitlines() == [
        "[Download] Batch 2/9 started: 1 file, 812.40 MiB",
        "  - GX010031.MP4 (GOPR12345678, 812.40 MiB)",
    ]


def test_validation_error_surfaces_underlying_exception_message() -> None:
    event = log_event(
        "error.validation",
        "Could not load media for re-merge",
        phase="selection",
        level="WARNING",
        error_type="ConnectionError",
        error_message="GoPro API request timed out",
    )

    assert event["message"] == (
        "Could not load media for re-merge: GoPro API request timed out"
    )
    assert "Error: GoPro API request timed out" in event["detail_lines"]


def test_sidecar_event_uses_sidecar_group() -> None:
    event = log_event(
        "sidecar.generation.completed",
        "Generated 3 XMP sidecar files.",
        phase="sidecars",
        level="SUCCESS",
        sidecar_count=3,
    )

    assert event["group"] == "sidecars"
    assert event["group_label"] == "Sidecars"
    assert event["summary"] == "Generated 3 XMP sidecar files"
