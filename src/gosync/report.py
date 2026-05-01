import json
from datetime import datetime
from pathlib import Path
from typing import Any

from gosync.constants import REPORTS_FOLDER, STATUS_DOWNLOADED, STATUS_FAILED
from gosync.manifest import MediaManifest
from gosync.state import media_records


def _record_identity(record: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(record.get("id") or ""),
        "filename": str(record.get("filename") or ""),
    }


def build_run_summary(
    state: dict[str, Any],
    manifest: MediaManifest,
    status: str,
    cache_sync_changes: list[dict[str, str]],
    report_path: Path | str | None = None,
) -> str:
    records = media_records(state)
    downloaded_count = sum(
        1 for record in records if record.get("download_status") == STATUS_DOWNLOADED
    )
    failed_count = sum(
        1 for record in records if record.get("download_status") == STATUS_FAILED
    )
    pending_count = len(records) - downloaded_count - failed_count
    sidecars_created_count = sum(
        1 for record in records if record.get("sidecar_status") == "complete"
    )
    retry_attempts = sum(int(record.get("retry_count") or 0) for record in records)

    lines = [
        "Run summary",
        f"Status: {status}",
        f"Total media: {len(records)}",
        f"Downloaded: {downloaded_count}",
        f"Pending: {pending_count}",
        f"Failed: {failed_count}",
        f"Sidecars created: {sidecars_created_count}",
        f"Retry attempts: {retry_attempts}",
        f"Skipped duplicates: {len(manifest.duplicates)}",
        f"Resume sync changes: {len(cache_sync_changes)}",
    ]
    if report_path:
        lines.append(f"Report: {report_path}")
    return "\n".join(lines)


def write_run_report(
    data_dir: Path,
    state: dict[str, Any],
    manifest: MediaManifest,
    status: str,
    cache_sync_changes: list[dict[str, str]],
) -> Path:
    reports_dir = data_dir / REPORTS_FOLDER
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"gosync-report-{timestamp}.json"

    records = media_records(state)
    downloaded = [
        _record_identity(record)
        for record in records
        if record.get("download_status") == STATUS_DOWNLOADED
    ]
    failed = [
        _record_identity(record)
        for record in records
        if record.get("download_status") == STATUS_FAILED
    ]
    pending = [
        _record_identity(record)
        for record in records
        if record.get("download_status") not in {STATUS_DOWNLOADED, STATUS_FAILED}
    ]
    sidecars_created = [
        _record_identity(record)
        for record in records
        if record.get("sidecar_status") == "complete"
    ]
    retry_attempts = sum(int(record.get("retry_count") or 0) for record in records)

    payload = {
        "status": status,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "total_media": len(records),
        "downloaded_count": len(downloaded),
        "pending_count": len(pending),
        "failed_count": len(failed),
        "sidecars_created_count": len(sidecars_created),
        "retry_attempts": retry_attempts,
        "duplicates": [
            {
                "id": duplicate.media_id,
                "filename": duplicate.filename,
                "key": duplicate.key,
            }
            for duplicate in manifest.duplicates
        ],
        "cache_sync_changes": cache_sync_changes,
        "downloaded": downloaded,
        "pending": pending,
        "failed": failed,
        "sidecars_created": sidecars_created,
    }
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path
