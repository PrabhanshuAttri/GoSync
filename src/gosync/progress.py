import threading
from dataclasses import dataclass, field
from datetime import datetime


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
    started_at: str = ""
    finished_at: str = ""
    events: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def increment(self, key: str, amount: int) -> None:
        with self.lock:
            setattr(self, key, getattr(self, key) + amount)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.message = message
            self.events.append(f"[{timestamp}] {message}")
            self.events = self.events[-80:]
        print(message, flush=True)

    def log_background(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.events.append(f"[{timestamp}] {message}")
            self.events = self.events[-80:]
        print(message, flush=True)

    def log_event(self, message: str, state_label: str | None = None) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.message = message
            if state_label:
                self.state_label = state_label
            self.events.append(f"[{timestamp}] {message}")
            self.events = self.events[-80:]
        print(message, flush=True)

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
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "events": list(self.events),
            }
