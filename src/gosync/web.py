import argparse
import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from gosync import __version__
from gosync.constants import STATUS_DOWNLOADED
from gosync.config import IS_PROD, ACCESS_LOGS
from gosync.config import resolve_inside_data_dir
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.manifest import (
    format_extension_summary,
    read_manifest_from_har,
    write_manifest,
    write_media_responses_dump,
)
from gosync.paths import media_download_path, sidecar_output_path
from gosync.progress import ProgressState
from gosync.runtime import get_runtime_paths, run_download_job
from gosync.sidecar import run_sidecar_job
from gosync.state import (
    completed_count as state_completed_count,
    create_or_update_state,
    sync_state_with_downloads,
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
    elif IS_PROD:
        if not any(
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

    def refresh_resume_counts() -> None:
        if PROGRESS.status == "running":
            return

        current_har = selected_har_file()
        if not current_har:
            return

        try:
            (
                data_path,
                har_path,
                output_dir,
                _sidecar_dir,
                state_file,
                manifest_file,
                media_dump_file,
            ) = get_runtime_paths(args, current_har)
            cache_key = (
                str(har_path),
                har_path.stat().st_mtime,
                str(state_file),
                state_file.stat().st_mtime if state_file.exists() else 0,
            )

            if RESUME_CACHE.get("key") == cache_key:
                total_ids = int(RESUME_CACHE["total_ids"])
                completed_count = int(RESUME_CACHE["completed_ids"])
            else:
                manifest = read_manifest_from_har(har_path)
                write_manifest(manifest, manifest_file, har_path)
                write_media_responses_dump(manifest, media_dump_file, har_path)
                create_or_update_state(state_file, manifest, data_path)
                state, _changes = sync_state_with_downloads(state_file, output_dir)
                total_ids = len(manifest.media)
                completed_count = state_completed_count(state)
                RESUME_CACHE.update(
                    key=cache_key,
                    total_ids=total_ids,
                    completed_ids=completed_count,
                )

            summary_key = (str(har_path), har_path.stat().st_mtime)
            if RESUME_CACHE.get("summary_key") != summary_key:
                manifest = read_manifest_from_har(har_path)
                PROGRESS.log_background(format_extension_summary(manifest))
                RESUME_CACHE["summary_key"] = summary_key

            PROGRESS.update(
                total_ids=total_ids,
                completed_ids=completed_count,
                pending_ids=max(total_ids - completed_count, 0),
                har_file=str(har_path),
            )
        except Exception:
            return

    def media_sidecar_rows() -> list[dict[str, str]]:
        output_dir = resolve_inside_data_dir(data_dir, args.output_folder).resolve()
        current_har = selected_har_file()
        state_records: list[dict[str, object]] = []

        if current_har:
            try:
                (
                    data_path,
                    har_path,
                    output_dir,
                    _sidecar_dir,
                    state_file,
                    manifest_file,
                    media_dump_file,
                ) = get_runtime_paths(args, current_har)
                cache_key = (
                    str(har_path),
                    har_path.stat().st_mtime,
                    str(state_file),
                    state_file.stat().st_mtime if state_file.exists() else 0,
                )

                if MEDIA_ID_CACHE.get("key") == cache_key:
                    state_records = list(MEDIA_ID_CACHE["state_records"])
                else:
                    manifest = read_manifest_from_har(har_path)
                    write_manifest(manifest, manifest_file, har_path)
                    write_media_responses_dump(manifest, media_dump_file, har_path)
                    create_or_update_state(state_file, manifest, data_path)
                    state, _changes = sync_state_with_downloads(state_file, output_dir)
                    media = state.get("media", {})
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
            fallback_sidecar_path = sidecar_files.get(sidecar_stem)
            status = (
                "downloading"
                if str(record.get("key") or "") in current_batch_keys
                else (
                    "downloaded"
                    if record.get("download_status") == STATUS_DOWNLOADED
                    or media_download_path(output_dir, filename).is_file()
                    else "pending"
                )
            )
            rows.append(
                {
                    "filename": filename,
                    "sidecar_filename": (
                        sidecar_path.name
                        if sidecar_path.is_file()
                        else (
                            fallback_sidecar_path.name
                            if fallback_sidecar_path
                            else sidecar_filename
                        )
                    ),
                    "status": status,
                }
            )
        return rows

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
            (
                data_path,
                har_path,
                output_dir,
                _sidecar_dir,
                state_file,
                manifest_file,
                media_dump_file,
            ) = get_runtime_paths(args, filename)
            manifest = read_manifest_from_har(har_path)
            write_manifest(manifest, manifest_file, har_path)
            write_media_responses_dump(manifest, media_dump_file, har_path)
            create_or_update_state(state_file, manifest, data_path)
            sync_state_with_downloads(state_file, output_dir)
            PROGRESS.log_background(format_extension_summary(manifest))
        except Exception as exc:
            LOGGER.warning("Failed to summarize uploaded HAR file %s: %s", filename, exc)
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

            (
                data_path,
                har_path,
                output_dir,
                sidecar_dir,
                state_file,
                manifest_file,
                media_dump_file,
            ) = get_runtime_paths(args, selected_har)
            manifest = read_manifest_from_har(har_path)
            write_manifest(manifest, manifest_file, har_path)
            write_media_responses_dump(manifest, media_dump_file, har_path)
            create_or_update_state(state_file, manifest, data_path)
            sync_state_with_downloads(state_file, output_dir)
            PROGRESS.update(
                notifications=[],
                sidecar_status="pending",
                sidecar_count=0,
                sidecar_dir=str(output_dir),
                sidecar_message="XMP sidecar generation queued.",
            )

            JOB_THREAD = threading.Thread(
                target=run_download_job,
                args=(args, PROGRESS, selected_har),
                daemon=True,
            )
            SIDECAR_THREAD = threading.Thread(
                target=run_sidecar_job,
                args=(har_path, output_dir, PROGRESS, manifest.media, state_file),
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

    return app
