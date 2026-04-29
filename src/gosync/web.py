import argparse
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from gosync.config import ACCESS_LOGS
from gosync.progress import ProgressState
from gosync.runtime import run_download_job


PROGRESS = ProgressState()
JOB_THREAD: threading.Thread | None = None
JOB_LOCK = threading.Lock()


def create_app(args: argparse.Namespace) -> Flask:
    if not ACCESS_LOGS:
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app = Flask(__name__)
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    def list_har_files() -> list[str]:
        return sorted(path.name for path in data_dir.glob("*.har"))

    @app.get("/")
    def index():
        har_files = list_har_files()
        current_har = args.har_file if args.har_file in har_files else ""
        if not current_har and "gopro.com.har" in har_files:
            current_har = "gopro.com.har"
        if not current_har and len(har_files) == 1:
            current_har = har_files[0]

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
        global JOB_THREAD

        selected_har = request.form.get("har_file") or args.har_file
        with JOB_LOCK:
            if JOB_THREAD and JOB_THREAD.is_alive():
                PROGRESS.log("A download is already running.")
                return redirect(url_for("index"))

            JOB_THREAD = threading.Thread(
                target=run_download_job,
                args=(args, PROGRESS, selected_har),
                daemon=True,
            )
            JOB_THREAD.start()

        return redirect(url_for("index"))

    @app.get("/status")
    def status():
        return jsonify(PROGRESS.snapshot())

    return app
