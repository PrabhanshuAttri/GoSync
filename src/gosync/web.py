import argparse
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from gosync.config import IS_PROD, ACCESS_LOGS
from gosync.config import resolve_inside_data_dir
from gosync.downloader import extract_ids, get_completed_ids
from gosync.progress import ProgressState
from gosync.runtime import run_download_job
from gosync.sidecar import run_sidecar_job


PROGRESS = ProgressState()
JOB_THREAD: threading.Thread | None = None
SIDECAR_THREAD: threading.Thread | None = None
JOB_LOCK = threading.Lock()
RESUME_CACHE: dict[str, object] = {}


def create_app(args: argparse.Namespace) -> Flask:
    if not ACCESS_LOGS:
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app = Flask(__name__)
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    def list_har_files() -> list[str]:
        return sorted(path.name for path in data_dir.glob("*.har"))

    def selected_har_file() -> str:
        har_files = list_har_files()
        current_har = args.har_file if args.har_file in har_files else ""
        if not current_har and "gopro.com.har" in har_files:
            current_har = "gopro.com.har"
        if not current_har and len(har_files) == 1:
            current_har = har_files[0]
        return current_har

    def refresh_resume_counts() -> None:
        if PROGRESS.status == "running":
            return

        current_har = selected_har_file()
        if not current_har:
            return

        try:
            har_path = data_dir / current_har
            completed_log = resolve_inside_data_dir(
                data_dir,
                args.completed_log,
            ).resolve()
            cache_key = (
                str(har_path),
                har_path.stat().st_mtime,
                str(completed_log),
                completed_log.stat().st_mtime if completed_log.exists() else 0,
            )

            if RESUME_CACHE.get("key") == cache_key:
                total_ids = int(RESUME_CACHE["total_ids"])
                completed_count = int(RESUME_CACHE["completed_ids"])
            else:
                ids = extract_ids(har_path)
                completed_ids = get_completed_ids(completed_log)
                total_ids = len(ids)
                completed_count = len(completed_ids.intersection(ids))
                RESUME_CACHE.update(
                    key=cache_key,
                    total_ids=total_ids,
                    completed_ids=completed_count,
                )

            PROGRESS.update(
                total_ids=total_ids,
                completed_ids=completed_count,
                pending_ids=max(total_ids - completed_count, 0),
                har_file=str(har_path),
            )
        except Exception:
            return

    @app.get("/")
    def index():
        har_files = list_har_files()
        current_har = selected_har_file()

        return render_template(
            "index.html",
            har_files=har_files,
            har_file=current_har or "No HAR file uploaded",
        )

    @app.post("/upload")
    def upload():
        uploaded = request.files.get("har_file")
        if not uploaded or not uploaded.filename:
            PROGRESS.log("No HAR file selected.")
            return redirect(url_for("index"))

        filename = secure_filename(uploaded.filename)
        if not filename.endswith(".har"):
            filename = f"{filename}.har"

        uploaded.save(data_dir / filename)
        args.har_file = filename
        PROGRESS.log_event(f"Uploaded HAR file: {filename}", state_label="Uploaded")
        return redirect(url_for("index"))

    @app.post("/start")
    def start():
        global JOB_THREAD, SIDECAR_THREAD

        selected_har = request.form.get("har_file") or args.har_file
        with JOB_LOCK:
            if JOB_THREAD and JOB_THREAD.is_alive():
                PROGRESS.log("A download is already running.")
                return redirect(url_for("index"))

            if not selected_har:
                PROGRESS.log("No HAR file selected.")
                return redirect(url_for("index"))

            har_path = Path(selected_har)
            if not har_path.is_absolute():
                har_path = data_dir / selected_har
            sidecar_dir = resolve_inside_data_dir(
                data_dir,
                args.sidecar_folder,
            ).resolve()
            PROGRESS.update(
                sidecar_status="pending",
                sidecar_count=0,
                sidecar_dir=str(sidecar_dir),
                sidecar_message="XMP sidecar generation queued.",
            )

            JOB_THREAD = threading.Thread(
                target=run_download_job,
                args=(args, PROGRESS, selected_har),
                daemon=True,
            )
            SIDECAR_THREAD = threading.Thread(
                target=run_sidecar_job,
                args=(har_path, sidecar_dir, PROGRESS),
                daemon=True,
            )
            SIDECAR_THREAD.start()
            JOB_THREAD.start()

        return redirect(url_for("index"))

    @app.post("/stop")
    def stop():
        if JOB_THREAD and JOB_THREAD.is_alive():
            PROGRESS.update(stop_requested=True, state_label="Stopping")
            PROGRESS.log_event("Stop requested. Finishing current cleanup...", "Stopping")
        else:
            PROGRESS.log_event("No active download job to stop.", PROGRESS.state_label)
        return redirect(url_for("index"))

    @app.get("/status")
    def status():
        refresh_resume_counts()
        return jsonify(PROGRESS.snapshot())

    return app
