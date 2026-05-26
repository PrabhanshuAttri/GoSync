import json
from pathlib import Path

from gosync.constants import STATUS_DOWNLOADED, STATUS_FAILED
from gosync.manifest import DuplicateMediaItem, MediaManifest
from gosync.report import build_run_summary, build_run_summary_event, write_run_report


def report_manifest(items) -> MediaManifest:
    return MediaManifest(
        media=items,
        duplicates=[
            DuplicateMediaItem(
                key="D_duplicate.mp4",
                media_id="D",
                filename="duplicate.mp4",
            )
        ],
        matching_entries=1,
        media_responses=[],
    )


def report_state(items) -> dict:
    return {
        "media": {
            items[0].key: {
                "key": items[0].key,
                "id": items[0].media_id,
                "filename": items[0].filename,
                "download_status": STATUS_DOWNLOADED,
                "sidecar_status": "complete",
                "retry_count": 1,
            },
            items[1].key: {
                "key": items[1].key,
                "id": items[1].media_id,
                "filename": items[1].filename,
                "download_status": STATUS_FAILED,
                "sidecar_status": "failed",
                "retry_count": 2,
            },
            items[2].key: {
                "key": items[2].key,
                "id": items[2].media_id,
                "filename": items[2].filename,
                "download_status": "pending",
                "sidecar_status": "pending",
                "retry_count": 0,
            },
        }
    }


def test_build_run_summary_counts_statuses_and_retries(make_media_item) -> None:
    items = [
        make_media_item("A", "done.mp4", 10),
        make_media_item("B", "failed.mp4", 20),
        make_media_item("C", "pending.jpg", 30),
    ]
    summary = build_run_summary(
        report_state(items),
        report_manifest(items),
        "complete",
        [{"id": "1", "filename": "a.mp4", "status": "found"}],
        Path("reports/run.json"),
    )

    assert "Total media: 3" in summary
    assert "Downloaded: 1" in summary
    assert "Pending: 1" in summary
    assert "Failed: 1" in summary
    assert "Sidecars created: 1" in summary
    assert "Retry attempts: 3" in summary
    assert "Skipped duplicates: 1" in summary
    assert "Resume sync changes: 1" in summary
    assert "Report: reports/run.json" in summary


def test_build_run_summary_event_is_compact_for_ui(make_media_item) -> None:
    items = [
        make_media_item("A", "done.mp4", 10),
        make_media_item("B", "failed.mp4", 20),
        make_media_item("C", "pending.jpg", 30),
    ]

    title, details = build_run_summary_event(
        report_state(items),
        report_manifest(items),
        "stopped",
        [{"id": "1", "filename": "a.mp4", "status": "found"}],
        Path("reports/run.json"),
    )

    assert title == "Run stopped"
    assert "1 of 3 media files downloaded" in details
    assert "1 pending" in details
    assert "1 failed" in details
    assert "1 sidecars created" in details
    assert "Report: reports/run.json" in details


def test_write_run_report_serializes_detailed_buckets(
    tmp_path: Path,
    make_media_item,
) -> None:
    items = [
        make_media_item("A", "done.mp4", 10),
        make_media_item("B", "failed.mp4", 20),
        make_media_item("C", "pending.jpg", 30),
    ]

    report_path = write_run_report(
        tmp_path,
        report_state(items),
        report_manifest(items),
        "stopped",
        [{"id": "A", "filename": "done.mp4", "status": "found"}],
    )

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "stopped"
    assert payload["downloaded_count"] == 1
    assert payload["failed_count"] == 1
    assert payload["pending_count"] == 1
    assert payload["sidecars_created_count"] == 1
    assert payload["retry_attempts"] == 3
    assert payload["duplicates"] == [
        {"id": "D", "filename": "duplicate.mp4", "key": "D_duplicate.mp4"}
    ]
    assert payload["downloaded"] == [{"id": "A", "filename": "done.mp4"}]
    assert payload["failed"] == [{"id": "B", "filename": "failed.mp4"}]
    assert payload["pending"] == [{"id": "C", "filename": "pending.jpg"}]
