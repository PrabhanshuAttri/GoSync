import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gosync.events import (
    GROUP_LABELS,
    event_presentation,
    log_event,
    ui_level,
    update_run_status,
)


@dataclass
class ProgressState:
    status: str = "idle"
    state_label: str = "Ready"
    message: str = "Ready"
    stop_requested: bool = False
    total_ids: int = 0
    completed_ids: int = 0
    pending_ids: int = 0
    total_batches: int = 0
    completed_batches: int = 0
    failed_batches: int = 0
    current_batch: int = 0
    current_batch_size: int = 0
    current_batch_keys: list[str] = field(default_factory=list)
    current_download_bytes: int = 0
    current_download_total: int = 0
    current_download_started_at: float = 0
    current_download_speed_bps: float = 0
    current_download_elapsed_seconds: float = 0
    output_dir: str = ""
    sidecar_dir: str = ""
    sidecar_status: str = "idle"
    sidecar_count: int = 0
    sidecar_message: str = ""
    har_file: str = ""
    report_path: str = ""
    job_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    phase: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _job_matches(self, job_id: str | None) -> bool:
        return not job_id or self.job_id == job_id

    def update(self, job_id_guard: str | None = None, **kwargs) -> bool:
        with self.lock:
            if not self._job_matches(job_id_guard):
                return False
            for key, value in kwargs.items():
                setattr(self, key, value)
            update_run_status(
                run_id=self.job_id,
                status=self.status,
                phase=self.phase,
                selected_count=self.total_ids - self.pending_ids,
                completed_count=self.completed_ids,
                failed_count=self.failed_batches,
                current_batch=self.current_batch,
                total_batches=self.total_batches,
                overall_progress_percent=round(
                    (self.completed_ids / self.total_ids) * 100,
                    2,
                )
                if self.total_ids
                else 0,
                current_file_progress_percent=round(
                    (self.current_download_bytes / self.current_download_total) * 100,
                    2,
                )
                if self.current_download_total
                else 0,
                download_speed_bytes_per_sec=self.current_download_speed_bps,
            )
            return True

    def increment(
        self,
        key: str,
        amount: int,
        job_id_guard: str | None = None,
    ) -> int | None:
        with self.lock:
            if not self._job_matches(job_id_guard):
                return None
            value = getattr(self, key) + amount
            setattr(self, key, value)
            return value

    def _append_structured_event(self, event: dict[str, Any]) -> None:
        normalized = normalize_event(event)
        if normalized["event"] == "download.file.progress":
            for index in range(len(self.events) - 1, -1, -1):
                existing = self.events[index]
                if (
                    existing.get("event") == "download.file.progress"
                    and existing.get("batch_index") == normalized.get("batch_index")
                    and existing.get("batch_total") == normalized.get("batch_total")
                    and existing.get("file_id") == normalized.get("file_id")
                ):
                    self.events[index] = normalized
                    break
            else:
                self.events.append(normalized)
        else:
            self.events.append(normalized)
        self.events = self.events[-80:]

    def emit_event(
        self,
        event: str,
        message: str,
        *,
        level: str = "INFO",
        phase: str | None = None,
        title: str | None = None,
        details: str = "",
        state_label: str | None = None,
        job_id_guard: str | None = None,
        set_message: bool = True,
        **fields: Any,
    ) -> bool:
        with self.lock:
            if not self._job_matches(job_id_guard):
                return False
            if set_message:
                self.message = message
            if state_label:
                self.state_label = state_label
            if phase:
                self.phase = phase

        record = log_event(
            event,
            message,
            level=level,
            phase=phase or self.phase,
            run_id=job_id_guard or self.job_id,
            title=title or state_label or message,
            details=details,
            sink=None,
            **fields,
        )
        with self.lock:
            if not self._job_matches(job_id_guard):
                return False
            self._append_structured_event(record)
        return True

    def _level_for_message(self, message: str) -> str:
        value = message.lower()
        if "failed" in value or "error" in value or "forbidden" in value:
            return "error"
        if "stopped" in value or "stop requested" in value:
            return "warning"
        if "downloaded" in value or "complete" in value:
            return "success"
        if "processing" in value or "downloading" in value or "extracting" in value:
            return "active"
        return "info"

    def log(self, message: str, job_id_guard: str | None = None) -> bool:
        return self.emit_event(
            "download.phase.started",
            message,
            level="ACTIVE",
            job_id_guard=job_id_guard,
        )

    def log_structured_event(
        self,
        title: str,
        *,
        level: str = "info",
        message: str = "",
        details: str = "",
        job_id_guard: str | None = None,
    ) -> bool:
        return self.emit_event(
            "app.ready",
            message,
            level=level.upper(),
            title=title,
            details=details,
            job_id_guard=job_id_guard,
            set_message=False,
            cli_message=title,
        )

    def log_event(
        self,
        message: str,
        state_label: str | None = None,
        job_id_guard: str | None = None,
    ) -> bool:
        level = (
            "ERROR"
            if state_label == "Failed"
            else "SUCCESS"
            if state_label == "Completed"
            else "WARNING"
            if state_label in {"Stopped", "Stopping"}
            else self._level_for_message(message).upper()
        )
        first_line, separator, details = message.partition("\n")
        return self.emit_event(
            "app.ready",
            first_line,
            level=level,
            state_label=state_label,
            title=state_label,
            details=details if separator else "",
            job_id_guard=job_id_guard,
        )

    def notify(
        self,
        level: str,
        title: str,
        message: str,
        job_id_guard: str | None = None,
    ) -> bool:
        timestamp = datetime.now()
        with self.lock:
            if not self._job_matches(job_id_guard):
                return False
            self.notifications.append(
                {
                    "id": f"{timestamp.timestamp():.6f}-{len(self.notifications)}",
                    "level": level,
                    "title": title,
                    "message": message,
                    "created_at": timestamp.isoformat(timespec="seconds"),
                }
            )
            self.notifications = self.notifications[-40:]
        return True

    def snapshot(self) -> dict:
        with self.lock:
            total_ids = self.total_ids or 0
            completed_ids = self.completed_ids or 0
            total_batches = self.total_batches or 0
            completed_batches = self.completed_batches or 0
            current_total = self.current_download_total or 0
            current_bytes = self.current_download_bytes or 0

            return {
                "status": self.status,
                "state_label": self.state_label,
                "message": self.message,
                "stop_requested": self.stop_requested,
                "total_ids": total_ids,
                "completed_ids": completed_ids,
                "pending_ids": self.pending_ids,
                "overall_percent": round((completed_ids / total_ids) * 100, 1)
                if total_ids
                else 0,
                "total_batches": total_batches,
                "completed_batches": completed_batches,
                "failed_batches": self.failed_batches,
                "batch_percent": round((completed_batches / total_batches) * 100, 1)
                if total_batches
                else 0,
                "current_batch": self.current_batch,
                "current_batch_size": self.current_batch_size,
                "current_batch_keys": list(self.current_batch_keys),
                "current_download_bytes": current_bytes,
                "current_download_total": current_total,
                "current_download_speed_bps": round(self.current_download_speed_bps, 2),
                "current_download_elapsed_seconds": round(
                    self.current_download_elapsed_seconds, 1
                ),
                "download_percent": round((current_bytes / current_total) * 100, 1)
                if current_total
                else 0,
                "output_dir": self.output_dir,
                "sidecar_dir": self.sidecar_dir,
                "sidecar_status": self.sidecar_status,
                "sidecar_count": self.sidecar_count,
                "sidecar_message": self.sidecar_message,
                "har_file": self.har_file,
                "report_path": self.report_path,
                "job_id": self.job_id,
                "run_id": self.job_id,
                "phase": self.phase,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "events": [normalize_event(event) for event in self.events],
                "notifications": list(self.notifications),
            }


def normalize_event(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        timestamp = str(event.get("timestamp") or event.get("created_at") or "")
        if timestamp and "T" in timestamp:
            time = timestamp.split("T", 1)[1].replace("Z", "").split(".", 1)[0]
        else:
            time = str(event.get("time") or "--:--:--")
        presentation = event_presentation(
            str(event.get("event") or ""),
            str(event.get("message") or ""),
            str(event.get("phase") or ""),
            event,
        )
        title = str(
            event.get("title")
            or event.get("summary")
            or presentation.get("summary")
            or event.get("message")
            or "Event"
        )
        details = str(event.get("details") or presentation.get("details") or "")
        normalized = {
            "id": str(event.get("id") or ""),
            "created_at": timestamp,
            "timestamp": timestamp,
            "time": time,
            "level": ui_level(str(event.get("level") or "INFO")),
            "severity": str(event.get("level") or "INFO").upper(),
            "event": str(event.get("event") or ""),
            "run_id": str(event.get("run_id") or ""),
            "phase": str(event.get("phase") or ""),
            "group": str(event.get("group") or presentation["group"]),
            "group_label": str(
                event.get("group_label")
                or GROUP_LABELS.get(str(event.get("group") or ""), "")
                or presentation["group_label"]
            ),
            "summary": str(event.get("summary") or presentation["summary"]),
            "title": title,
            "message": str(event.get("message") or presentation.get("message") or ""),
            "details": details,
            "detail_lines": list(
                event.get("detail_lines") or presentation.get("detail_lines") or []
            ),
            "meta": list(event.get("meta") or presentation.get("meta") or []),
            "files": list(event.get("files") or presentation.get("files") or []),
        }
        for key, value in event.items():
            normalized.setdefault(key, value)
        return normalized

    text = str(event or "")
    time = "--:--:--"
    message = text
    if text.startswith("[") and "] " in text:
        time, message = text[1:].split("] ", 1)
    title, separator, details = message.partition("\n")
    return {
        "id": "",
        "created_at": "",
        "time": time,
        "level": "info",
        "group": "setup",
        "group_label": "Setup",
        "summary": title or "Event",
        "title": title or "Event",
        "message": "",
        "details": details if separator else "",
        "detail_lines": [details] if separator and details else [],
        "meta": [],
        "files": [],
    }
