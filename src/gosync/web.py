import argparse
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from gosync import __version__
from gosync.config import ACCESS_LOGS, IS_PROD, resolve_inside_data_dir
from gosync.constants import STATUS_DOWNLOADED
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.paths import safe_child_path, sidecar_output_path
from gosync.progress import ProgressState
from gosync.runtime import (
    format_manifest_state_summary,
    get_runtime_paths,
    prepare_paths_manifest_state,
    prepare_runtime_manifest_state,
    run_download_job,
    runtime_cache_key,
)
from gosync.sidecar import run_sidecar_job
from gosync.state import (
    completed_count as state_completed_count,
)
from gosync.state import (
    downloaded_filename_index,
    media_file_exists,
)

PROGRESS = ProgressState()
JOB_THREAD: threading.Thread | None = None
SIDECAR_THREAD: threading.Thread | None = None
JOB_LOCK = threading.Lock()
RESUME_CACHE: dict[str, object] = {}
MEDIA_ID_CACHE: dict[str, object] = {}


class StatusAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        quiet_poll_paths = ("/status", "/sidecars")
        return not any(
            f'"GET {path} ' in message or f'"GET {path}?' in message
            for path in quiet_poll_paths
        )


def create_app(args: argparse.Namespace) -> Flask:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_file = configure_file_logging(data_dir)
    LOGGER.info("File logging enabled at %s", log_file)

    werkzeug_logger = logging.getLogger("werkzeug")
    if not ACCESS_LOGS:
        werkzeug_logger.setLevel(logging.ERROR)
    elif IS_PROD and not any(
        isinstance(log_filter, StatusAccessLogFilter)
        for log_filter in werkzeug_logger.filters
    ):
        werkzeug_logger.addFilter(StatusAccessLogFilter())

    app = Flask(__name__)
    LOGGER.info("Starting GoSync web app. data_dir=%s", data_dir)

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

    def refresh_resume_counts(log_summary: bool = False) -> None:
        if PROGRESS.status == "running":
            return

        current_har = selected_har_file()
        if not current_har:
            return

        try:
            paths = get_runtime_paths(args, current_har)
            cache_key = runtime_cache_key(paths)
            summary = None

            if RESUME_CACHE.get("key") == cache_key:
                total_ids = int(RESUME_CACHE["total_ids"])
                completed_count = int(RESUME_CACHE["completed_ids"])
            else:
                prepared = prepare_paths_manifest_state(paths)
                summary = format_manifest_state_summary(
                    prepared.manifest,
                    prepared.state,
                )
                manifest = prepared.manifest
                state = prepared.state
                total_ids = len(manifest.media)
                completed_count = state_completed_count(state)
                RESUME_CACHE.update(
                    key=cache_key,
                    total_ids=total_ids,
                    completed_ids=completed_count,
                )

            summary_key = cache_key
            if log_summary and RESUME_CACHE.get("summary_key") != summary_key:
                if summary is None:
                    prepared = prepare_paths_manifest_state(paths)
                    summary = format_manifest_state_summary(
                        prepared.manifest,
                        prepared.state,
                    )
                PROGRESS.log_background(summary)
                RESUME_CACHE["summary_key"] = summary_key

            PROGRESS.update(
                total_ids=total_ids,
                completed_ids=completed_count,
                pending_ids=max(total_ids - completed_count, 0),
                har_file=str(paths.har_path),
            )
        except Exception:
            return

    def media_sidecar_rows() -> list[dict[str, str]]:
        output_dir = resolve_inside_data_dir(data_dir, args.output_folder).resolve()
        current_har = selected_har_file()
        state_records: list[dict[str, object]] = []

        if current_har:
            try:
                paths = get_runtime_paths(args, current_har)
                output_dir = paths.output_dir
                cache_key = runtime_cache_key(paths)

                if MEDIA_ID_CACHE.get("key") == cache_key:
                    state_records = list(MEDIA_ID_CACHE["state_records"])
                else:
                    prepared = prepare_paths_manifest_state(paths)
                    media = prepared.state.get("media", {})
                    state_records = (
                        list(media.values()) if isinstance(media, dict) else []
                    )
                    MEDIA_ID_CACHE.update(
                        key=cache_key,
                        state_records=state_records,
                    )
            except Exception:
                state_records = []

        sidecar_files = {
            path.name.removesuffix(".xmp"): path
            for path in output_dir.glob("*/*.xmp")
            if path.is_file()
        } if output_dir.exists() else {}

        rows = []
        existing_filenames = downloaded_filename_index(output_dir)
        current_batch_keys = set(PROGRESS.snapshot().get("current_batch_keys", []))
        for record in sorted(
            state_records,
            key=lambda item: str(item.get("filename", "")).lower(),
        ):
            filename = str(record.get("filename") or "")
            sidecar_filename = str(record.get("sidecar_filename") or "")
            sidecar_stem = sidecar_filename.removesuffix(".xmp")
            sidecar_path = sidecar_output_path(
                output_dir,
                filename,
                sidecar_filename,
            )
            sidecar_path_exists = (
                safe_child_path(output_dir, sidecar_path) and sidecar_path.is_file()
            )
            fallback_sidecar_path = sidecar_files.get(sidecar_stem)
            status = (
                "downloading"
                if str(record.get("key") or "") in current_batch_keys
                else (
                    "downloaded"
                    if record.get("download_status") == STATUS_DOWNLOADED
                    or media_file_exists(output_dir, filename, existing_filenames)
                    else "pending"
                )
            )
            rows.append(
                {
                    "key": str(record.get("key") or ""),
                    "filename": filename,
                    "sidecar_filename": (
                        sidecar_path.name
                        if sidecar_path_exists
                        else (
                            fallback_sidecar_path.name
                            if fallback_sidecar_path
                            else sidecar_filename
                        )
                    ),
                    "file_size": record.get("file_size"),
                    "status": status,
                }
            )
        return rows

    def parse_files_per_batch() -> int | None:
        raw_value = (request.form.get("files_per_batch") or "").strip()
        if not raw_value:
            return None
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ValueError("Files per batch must be a positive number.") from exc
        if value < 1:
            raise ValueError("Files per batch must be at least 1.")
        return value

    @app.get("/")
    def index():
        har_files = list_har_files()
        current_har = selected_har_file()

        return render_template(
            "index.html",
            app_version=__version__,
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
        try:
            prepare_runtime_manifest_state(args, filename)
        except Exception as exc:
            LOGGER.warning(
                "Failed to summarize uploaded HAR file %s: %s",
                filename,
                exc,
            )
            PROGRESS.log_background(f"Could not summarize uploaded HAR: {exc}")
        return redirect(url_for("index"))

    @app.post("/start")
    def start():
        global JOB_THREAD, SIDECAR_THREAD

        selected_har = request.form.get("har_file") or args.har_file
        with JOB_LOCK:
            if (JOB_THREAD and JOB_THREAD.is_alive()) or (
                SIDECAR_THREAD and SIDECAR_THREAD.is_alive()
            ):
                PROGRESS.log("A job is already running.")
                return redirect(url_for("index"))

            if not selected_har:
                PROGRESS.log("No HAR file selected.")
                return redirect(url_for("index"))

            prepared = prepare_runtime_manifest_state(args, selected_har)
            selected_keys = {
                value
                for value in request.form.getlist("selected_media_keys")
                if value
            }
            valid_keys = {item.key for item in prepared.manifest.media}
            invalid_keys = selected_keys - valid_keys
            if invalid_keys:
                PROGRESS.log_event(
                    "Selected media no longer matches the HAR. Refresh and try again.",
                    "Ready",
                )
                return redirect(url_for("index"))
            if not selected_keys:
                PROGRESS.log_event(
                    "Select at least one pending media file before starting.",
                    "Ready",
                )
                return redirect(url_for("index"))
            try:
                files_per_batch = parse_files_per_batch()
            except ValueError as exc:
                PROGRESS.log_event(str(exc), "Ready")
                return redirect(url_for("index"))

            paths = prepared.paths
            selected_media = [
                item for item in prepared.manifest.media if item.key in selected_keys
            ]
            PROGRESS.update(
                notifications=[],
                sidecar_status="pending",
                sidecar_count=0,
                sidecar_dir=str(paths.output_dir),
                sidecar_message="XMP sidecar generation queued.",
            )

            JOB_THREAD = threading.Thread(
                target=run_download_job,
                args=(args, PROGRESS, selected_har, selected_keys, files_per_batch),
                daemon=True,
            )
            SIDECAR_THREAD = threading.Thread(
                target=run_sidecar_job,
                args=(
                    paths.har_path,
                    paths.output_dir,
                    PROGRESS,
                    selected_media,
                    paths.state_file,
                ),
                daemon=True,
            )
            SIDECAR_THREAD.start()
            JOB_THREAD.start()

        return redirect(url_for("index"))

    @app.post("/stop")
    def stop():
        if JOB_THREAD and JOB_THREAD.is_alive():
            PROGRESS.update(stop_requested=True, state_label="Stopping")
            PROGRESS.log_event(
                "Stop requested. Finishing current cleanup...",
                "Stopping",
            )
        else:
            PROGRESS.log_event("No active download job to stop.", PROGRESS.state_label)
        return redirect(url_for("index"))

    @app.get("/status")
    def status():
        refresh_resume_counts()
        return jsonify(PROGRESS.snapshot())

    @app.get("/sidecars")
    def sidecars():
        return jsonify({"items": media_sidecar_rows()})

    refresh_resume_counts(log_summary=True)
    return app
