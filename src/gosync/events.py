import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("gosync.events")

RECENT_EVENTS: deque[dict[str, Any]] = deque(maxlen=500)
RUN_STATUS: dict[str, Any] = {}
SENSITIVE_FIELD_NAMES = {
    "authorization",
    "cookie",
    "headers",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "secret_key",
    "signed_url",
    "url",
}
ALLOWED_EVENTS = {
    "app.starting",
    "app.ready",
    "web.server.started",
    "media.scan.started",
    "media.scan.completed",
    "media.selection.empty",
    "media.selection.completed",
    "har.scan.started",
    "har.scan.completed",
    "auth.session.reused",
    "download.phase.started",
    "download.plan.created",
    "download.batch.configured",
    "download.batch.started",
    "download.batch.completed",
    "download.file.started",
    "download.file.progress",
    "download.file.completed",
    "download.file.skipped",
    "download.file.failed",
    "download.chapter.merge_started",
    "download.chapter.merged",
    "download.chapter.merge_skipped",
    "download.chapter.merge_failed",
    "download.chapter.metadata_copied",
    "download.chapter.metadata_skipped",
    "download.chapter.metadata_failed",
    "download.chapter.local_reuse",
    "download.chapter.corrupt_merge_detected",
    "download.chapter.size_delta_near_tolerance",
    "download.chapter.local_merge_batch_deferred",
    "download.chapter.remerge_started",
    "download.chapter.remerged",
    "sidecar.generation.started",
    "sidecar.generation.completed",
    "sidecar.generation.failed",
    "sidecar.item.completed",
    "telemetry.generation.started",
    "telemetry.generation.completed",
    "telemetry.track.completed",
    "telemetry.mediainfo.completed",
    "run.stop_requested",
    "run.cleanup.started",
    "run.cleanup.completed",
    "run.stopped",
    "error.validation",
    "error.auth",
    "error.network",
    "error.filesystem",
    "error.unhandled",
}
GROUP_LABELS = {
    "setup": "Setup",
    "scan": "Media Scan",
    "auth": "Auth",
    "download": "Download",
    "telemetry": "Telemetry",
    "sidecars": "Sidecars",
    "finish": "Finish",
}
EVENT_GROUPS = {
    "app.starting": "setup",
    "app.ready": "setup",
    "web.server.started": "setup",
    "media.scan.started": "scan",
    "media.scan.completed": "scan",
    "media.selection.empty": "scan",
    "media.selection.completed": "scan",
    "har.scan.started": "scan",
    "har.scan.completed": "scan",
    "auth.session.reused": "auth",
    "telemetry.generation.started": "telemetry",
    "telemetry.generation.completed": "telemetry",
    "telemetry.track.completed": "telemetry",
    "telemetry.mediainfo.completed": "telemetry",
    "sidecar.generation.started": "sidecars",
    "sidecar.generation.completed": "sidecars",
    "sidecar.generation.failed": "sidecars",
    "sidecar.item.completed": "sidecars",
    "run.cleanup.completed": "finish",
    "run.stopped": "finish",
}
PHASE_GROUPS = {
    "auth": "auth",
    "cleanup": "finish",
    "download": "download",
    "scan": "scan",
    "selection": "scan",
    "telemetry": "telemetry",
    "sidecars": "sidecars",
}
LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "ACTIVE": logging.INFO,
    "SUCCESS": logging.INFO,
}


def timestamp_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def normalize_level(level: str) -> str:
    normalized = str(level or "INFO").upper()
    if normalized == "WARN":
        return "WARNING"
    return normalized if normalized in LEVELS else "INFO"


def ui_level(level: str) -> str:
    normalized = normalize_level(level)
    if normalized == "ERROR" or normalized == "CRITICAL":
        return "error"
    if normalized == "WARNING":
        return "warning"
    if normalized == "SUCCESS":
        return "success"
    if normalized == "ACTIVE":
        return "active"
    return "info"


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return redact_fields(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in fields.items():
        normalized = key.lower()
        if normalized in SENSITIVE_FIELD_NAMES or any(
            marker in normalized for marker in ("cookie", "token", "secret")
        ):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = redact_value(value)
    return redacted


def format_size(size_bytes: Any) -> str:
    try:
        size = float(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return "unknown size"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else plural or f"{singular}s"


def group_for_event(event: str, phase: str | None) -> str:
    if event.startswith("download."):
        return "download"
    if event.startswith("error.auth"):
        return "auth"
    if event.startswith("error."):
        return PHASE_GROUPS.get(str(phase or ""), "download")
    return EVENT_GROUPS.get(event, PHASE_GROUPS.get(str(phase or ""), "setup"))


def normalize_file_payload(file_info: Any) -> dict[str, Any] | None:
    if not isinstance(file_info, dict):
        return None
    file_name = str(file_info.get("file_name") or file_info.get("filename") or "")
    file_id = str(file_info.get("file_id") or file_info.get("id") or "")
    file_size_bytes = file_info.get("file_size_bytes", file_info.get("file_size"))
    file_size_human = str(
        file_info.get("file_size_human") or format_size(file_size_bytes)
    )
    if not file_name and not file_id:
        return None
    return {
        "file_name": file_name,
        "file_id": file_id,
        "file_size_bytes": file_size_bytes,
        "file_size_human": file_size_human,
    }


def event_files(fields: dict[str, Any]) -> list[dict[str, Any]]:
    raw_files = fields.get("files")
    files: list[dict[str, Any]] = []
    if isinstance(raw_files, list):
        for raw_file in raw_files:
            file_payload = normalize_file_payload(raw_file)
            if file_payload:
                files.append(file_payload)
    elif isinstance(raw_files, dict):
        file_payload = normalize_file_payload(raw_files)
        if file_payload:
            files.append(file_payload)

    if not files and (fields.get("file_name") or fields.get("file_id")):
        file_payload = normalize_file_payload(fields)
        if file_payload:
            files.append(file_payload)
    return files


def file_detail_lines(files: list[dict[str, Any]]) -> list[str]:
    return [
        f"{file_info.get('file_name') or file_info.get('file_id')} · "
        f"{file_info.get('file_size_human') or 'unknown size'}"
        for file_info in files
    ]


def cli_file_lines(files: list[dict[str, Any]]) -> list[str]:
    lines = []
    for file_info in files:
        name = file_info.get("file_name") or "unknown file"
        file_id = file_info.get("file_id") or "unknown id"
        size = file_info.get("file_size_human") or "unknown size"
        lines.append(f"  - {name} ({file_id}, {size})")
    return lines


def files_total_human(files: list[dict[str, Any]]) -> str:
    total = 0
    for file_info in files:
        try:
            total += int(file_info.get("file_size_bytes") or 0)
        except (TypeError, ValueError):
            return ""
    return format_size(total) if total else ""


def batch_label(fields: dict[str, Any]) -> str:
    index = fields.get("batch_index")
    total = fields.get("batch_total")
    return f"Batch {index}/{total}" if index and total else "Batch"


def event_presentation(
    event: str,
    message: str,
    phase: str | None,
    fields: dict[str, Any],
) -> dict[str, Any]:
    group = str(fields.get("group") or group_for_event(event, phase))
    files = event_files(fields)
    file_count = len(files) or int(fields.get("files_in_batch") or 0)
    file_total = files_total_human(files)
    details = str(fields.get("details") or "")
    detail_lines = list(fields.get("detail_lines") or [])
    if files and not detail_lines:
        detail_lines = file_detail_lines(files)

    meta = list(fields.get("meta") or [])
    if fields.get("batch_index") and fields.get("batch_total"):
        meta.append(batch_label(fields))
    if file_count:
        meta.append(f"{file_count} {pluralize(file_count, 'file')}")
    if file_total:
        meta.append(file_total)

    summary = str(fields.get("summary") or fields.get("title") or message or "Event")
    ui_message = message

    if event == "media.scan.completed" and fields.get("total_count") is not None:
        counts = fields.get("counts_by_extension") or {}
        count_text = ", ".join(
            f"{extension} {count:,}" for extension, count in counts.items()
        )
        suffix = f": {count_text}" if count_text else ""
        summary = f"Found {int(fields['total_count']):,} media files{suffix}"
    elif event == "auth.session.reused":
        auth_mode = fields.get("auth_mode", "browser session")
        summary = f"Browser session reused using {auth_mode} headers"
    elif event == "download.phase.started":
        summary = (
            f"Download phase started: {int(fields.get('remaining_count') or 0):,} "
            "files remaining"
        )
    elif event == "download.batch.configured":
        summary = (
            "Batch limits configured: "
            f"{fields.get('batch_size_cap_human') or 'unknown size'} max batch size"
        )
    elif event == "download.batch.started":
        summary = f"{batch_label(fields)} started"
        count = file_count or int(fields.get("files_in_batch") or 0)
        ui_message = (
            f"Preparing {count} {pluralize(count, 'file')} for download."
        )
    elif event == "download.file.progress":
        percent = fields.get("progress_percent", 0)
        speed = format_size(fields.get("speed_bytes_per_sec"))
        summary = f"{batch_label(fields)} downloading: {percent}% at {speed}/s"
    elif event == "download.batch.completed":
        summary = f"{batch_label(fields)} complete"
        count = file_count or fields.get("files_in_batch", 0)
        ui_message = f"{count} files downloaded."
    elif event == "download.file.failed":
        summary = f"{batch_label(fields)} failed"
        retry = str(fields.get("retry_message") or "").rstrip(".")
        error = str(fields.get("error_message") or "").strip()
        ui_message = ". ".join(part for part in (error, retry) if part)
        if error:
            detail_lines.append(f"Error: {error}")
        if retry:
            detail_lines.append(f"Next: {retry}")
    elif event == "sidecar.generation.started":
        summary = "Generating XMP sidecars"
    elif event == "sidecar.generation.completed":
        written = int(fields.get("sidecar_count") or fields.get("written_count") or 0)
        summary = f"Generated {written} XMP sidecar {pluralize(written, 'file')}"
    elif event == "sidecar.generation.failed":
        summary = "XMP sidecar generation failed"
        if fields.get("error_message"):
            detail_lines.append(f"Error: {fields['error_message']}")
    elif event == "run.cleanup.completed":
        summary = fields.get("summary") or "Run complete"
    elif event == "run.stopped":
        summary = "Run stopped safely"
    elif event == "error.validation" and fields.get("error_message"):
        error = str(fields["error_message"]).strip()
        if error:
            ui_message = f"{message}: {error}" if message else error
            detail_lines.append(f"Error: {error}")

    return {
        "group": group,
        "group_label": GROUP_LABELS.get(group, group.title()),
        "summary": summary,
        "detail_lines": detail_lines,
        "meta": list(dict.fromkeys(str(value) for value in meta if value)),
        "files": files,
        "message": ui_message,
        "details": details,
    }


def format_cli_message(event: str, message: str, fields: dict[str, Any]) -> str:
    cli_message = fields.pop("cli_message", None)
    if cli_message:
        presentation = event_presentation(
            event,
            str(cli_message),
            fields.get("phase"),
            fields,
        )
        return f"[{presentation['group_label']}] {cli_message}"

    presentation = event_presentation(event, message, fields.get("phase"), fields)
    summary = str(presentation["summary"])
    if event.startswith("download.batch."):
        batch = batch_label(fields)
        meta = [value for value in presentation.get("meta", []) if value != batch]
        if meta:
            summary = f"{summary}: {', '.join(meta)}"
    if event == "download.batch.completed" and presentation.get("message"):
        summary = f"{presentation['summary']}: {presentation['message']}"
    if event == "download.file.failed" and fields.get("error_message"):
        summary = f"{presentation['summary']}: {fields['error_message']}"
    lines = [f"[{presentation['group_label']}] {summary}"]
    files = presentation.get("files") or []
    if event in {
        "download.batch.started",
        "download.file.failed",
    }:
        lines.extend(cli_file_lines(files))
    if event == "download.file.failed" and fields.get("retry_message"):
        lines.append(f"[Download] Retry queued: {fields['retry_message']}")
    return "\n".join(lines)


def log_event(
    event: str,
    message: str,
    level: str = "INFO",
    phase: str | None = None,
    run_id: str | None = None,
    sink: Callable[[dict[str, Any]], None] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    normalized_level = normalize_level(level)
    clean_fields = redact_fields(fields)
    phase_value = phase or str(clean_fields.get("phase") or "")
    presentation = event_presentation(event, message, phase_value, clean_fields)
    clean_fields.update(presentation)
    cli_message = format_cli_message(event, message, dict(clean_fields))
    record: dict[str, Any] = {
        "timestamp": timestamp_utc(),
        "level": normalized_level,
        "event": event,
        "message": presentation["message"],
        "run_id": run_id or "",
        "phase": phase_value,
        **clean_fields,
    }
    if event not in ALLOWED_EVENTS:
        record["unstable_event_name"] = True

    RECENT_EVENTS.append(record)
    LOGGER.log(LEVELS[normalized_level], cli_message, extra={"gosync_event": record})
    if sink:
        sink(record)
    return record


class ProgressEventThrottle:
    def __init__(self, interval_seconds: float = 0.5, percent_step: float = 1.0):
        self.interval_seconds = interval_seconds
        self.percent_step = percent_step
        self.last_emit_time = 0.0
        self.last_percent: float | None = None

    def should_emit(
        self,
        downloaded_bytes: int,
        total_bytes: int,
        *,
        now: float | None = None,
        force: bool = False,
    ) -> bool:
        if force:
            return True
        if total_bytes <= 0:
            percent = 0.0
        else:
            percent = round((downloaded_bytes / total_bytes) * 100, 2)
        current_time = time.monotonic() if now is None else now
        if percent >= 100:
            return True
        if self.last_percent is None:
            return True
        if current_time - self.last_emit_time >= self.interval_seconds:
            return True
        return abs(percent - self.last_percent) >= self.percent_step

    def mark_emitted(
        self,
        downloaded_bytes: int,
        total_bytes: int,
        *,
        now: float | None = None,
    ) -> None:
        self.last_emit_time = time.monotonic() if now is None else now
        self.last_percent = (
            round((downloaded_bytes / total_bytes) * 100, 2)
            if total_bytes > 0
            else 0.0
        )


def update_run_status(**fields: Any) -> dict[str, Any]:
    RUN_STATUS.update(redact_fields(fields))
    return dict(RUN_STATUS)


def recent_events(run_id: str | None = None) -> list[dict[str, Any]]:
    events = list(RECENT_EVENTS)
    if not run_id:
        return events
    return [event for event in events if event.get("run_id") == run_id]


def current_run_status() -> dict[str, Any]:
    return dict(RUN_STATUS)
