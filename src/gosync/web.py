import argparse
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from gosync import __version__
from gosync.chapters import (
    MERGE_STATUS_REMERGE_ELIGIBLE,
    build_chapter_directory_listing,
    compute_merge_status,
)
from gosync.config import ACCESS_LOGS, IS_PROD
from gosync.constants import (
    AUTH_METHOD_API_TOKEN,
    AUTH_METHOD_HAR,
    STATUS_COMPLETE,
    STATUS_DOWNLOADED,
)
from gosync.events import current_run_status, recent_events
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.paths import media_download_path
from gosync.progress import ProgressState
from gosync.runtime import (
    auth_source_label,
    format_manifest_state_summary,
    get_runtime_paths,
    prepare_paths_manifest_state,
    prepare_runtime_manifest_state,
    resolve_headers,
    run_download_job,
    run_metadata_update_job,
    run_remerge_job,
    runtime_cache_key,
)
from gosync.state import (
    completed_count as state_completed_count,
)
from gosync.state import downloaded_keys, load_state, pending_keys

PROGRESS = ProgressState()
JOB_THREAD: threading.Thread | None = None
SIDECAR_THREAD: threading.Thread | None = None
TELEMETRY_THREAD: threading.Thread | None = None
MERGE_THREAD: threading.Thread | None = None
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
    current_har_name = args.har_file
    current_auth_method = (
        getattr(args, "auth_method", None) or AUTH_METHOD_HAR
    ).strip().lower()
    if current_auth_method not in (AUTH_METHOD_HAR, AUTH_METHOD_API_TOKEN):
        current_auth_method = AUTH_METHOD_HAR
    current_auth_token = (getattr(args, "auth_token", None) or "").strip()
    current_user_id = (getattr(args, "user_id", None) or "").strip()
    current_download_telemetry = bool(getattr(args, "download_telemetry", False))
    current_create_xmp_sidecars = bool(getattr(args, "create_xmp_sidecars", True))

    def effective_args() -> argparse.Namespace:
        merged = vars(args).copy()
        merged.update(
            auth_method=current_auth_method,
            auth_token=current_auth_token or None,
            user_id=current_user_id or None,
            download_telemetry=current_download_telemetry,
            create_xmp_sidecars=current_create_xmp_sidecars,
        )
        return argparse.Namespace(**merged)

    def list_har_files() -> list[str]:
        return sorted(path.name for path in data_dir.glob("*.har"))

    def selected_har_file() -> str:
        har_files = list_har_files()
        current_har = current_har_name if current_har_name in har_files else ""
        if not current_har and "gopro.com.har" in har_files:
            current_har = "gopro.com.har"
        if not current_har and len(har_files) == 1:
            current_har = har_files[0]
        return current_har

    def active_har_arg() -> str | None:
        if current_auth_method == AUTH_METHOD_API_TOKEN:
            return None
        return selected_har_file() or None

    def has_active_auth() -> bool:
        if current_auth_method == AUTH_METHOD_API_TOKEN:
            return bool(current_auth_token)
        return bool(selected_har_file())

    def refresh_resume_counts(log_summary: bool = False) -> None:
        if PROGRESS.status == "running":
            return

        if not has_active_auth():
            return

        try:
            paths = get_runtime_paths(effective_args(), active_har_arg())
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
                prepare_paths_manifest_state(paths, PROGRESS)
                RESUME_CACHE["summary_key"] = summary_key

            PROGRESS.update(
                total_ids=total_ids,
                completed_ids=completed_count,
                pending_ids=max(total_ids - completed_count, 0),
                har_file=auth_source_label(paths.auth),
            )
        except Exception as exc:
            LOGGER.warning("Failed to refresh resume counts: %s", exc)
            return

    def media_sidecar_rows() -> list[dict[str, str]]:
        state_records: list[dict[str, object]] = []
        cache_key = None
        output_dir: Path | None = None

        if has_active_auth():
            try:
                paths = get_runtime_paths(effective_args(), active_har_arg())
                output_dir = paths.output_dir
                progress_snapshot = PROGRESS.snapshot()
                cache_key = (
                    runtime_cache_key(paths),
                    tuple(sorted(progress_snapshot.get("current_batch_keys", []))),
                    progress_snapshot.get("sidecar_status", ""),
                    progress_snapshot.get("sidecar_count", 0),
                )
                if MEDIA_ID_CACHE.get("key") == cache_key:
                    return list(MEDIA_ID_CACHE["rows"])

                if paths.state_file.exists():
                    state = load_state(paths.state_file)
                else:
                    state = prepare_paths_manifest_state(paths).state
                media = state.get("media", {})
                state_records = list(media.values()) if isinstance(media, dict) else []
            except Exception as exc:
                LOGGER.warning("Failed to prepare media table rows: %s", exc)
                state_records = []

        # Built once per request (not once per chaptered row) so a library
        # with many chaptered items doesn't pay for repeated directory
        # scans -- see gosync.chapters.build_chapter_directory_listing.
        chapter_listing = (
            build_chapter_directory_listing(output_dir) if output_dir else []
        )

        rows = []
        current_batch_keys = set(PROGRESS.snapshot().get("current_batch_keys", []))
        for record in sorted(
            state_records,
            key=lambda item: str(item.get("filename", "")).lower(),
        ):
            filename = str(record.get("filename") or "")
            item_count = int(record.get("item_count") or 1)
            status = (
                "downloading"
                if str(record.get("key") or "") in current_batch_keys
                else "downloaded"
                if record.get("download_status") == STATUS_DOWNLOADED
                else "pending"
            )
            merge_status = None
            remerge_eligible = False
            if item_count > 1 and output_dir is not None:
                pseudo_item = SimpleNamespace(
                    filename=filename, metadata={"item_count": item_count}
                )
                merge_status = compute_merge_status(
                    pseudo_item,
                    media_download_path(output_dir, filename),
                    chapter_listing,
                )
                remerge_eligible = merge_status in MERGE_STATUS_REMERGE_ELIGIBLE
            rows.append(
                {
                    "key": str(record.get("key") or ""),
                    "filename": filename,
                    "item_count": item_count,
                    "file_size": record.get("file_size"),
                    "captured_at": str(record.get("captured_at") or ""),
                    "status": status,
                    "merge_status": merge_status,
                    "remerge_eligible": remerge_eligible,
                }
            )
        if cache_key is not None:
            MEDIA_ID_CACHE.update(key=cache_key, rows=rows)
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

    def job_action_response():
        if request.headers.get("X-Requested-With") == "fetch":
            return "", 204
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        har_files = list_har_files()
        current_har = selected_har_file()

        return render_template(
            "index.html",
            app_version=__version__,
            har_files=har_files,
            har_file=current_har or "No HAR file uploaded",
            auth_method=current_auth_method,
            auth_token_set=bool(current_auth_token),
            user_id=current_user_id,
            download_telemetry=current_download_telemetry,
            create_xmp_sidecars=current_create_xmp_sidecars,
        )

    def apply_settings_form(form) -> None:
        nonlocal current_auth_method, current_auth_token, current_user_id
        nonlocal current_download_telemetry, current_create_xmp_sidecars

        token = (form.get("auth_token") or "").strip()
        user_id = (form.get("user_id") or "").strip()
        download_telemetry = form.get("download_telemetry") == "on"
        create_xmp_sidecars = form.get("create_xmp_sidecars") == "on"

        # Leave a previously-saved token in place if the (masked) field was
        # submitted empty, so re-saving other settings doesn't force
        # re-pasting the token every time.
        effective_token = token or current_auth_token

        current_auth_method = (
            AUTH_METHOD_API_TOKEN if effective_token and user_id else AUTH_METHOD_HAR
        )
        if token:
            current_auth_token = token
        current_user_id = user_id
        current_download_telemetry = download_telemetry
        current_create_xmp_sidecars = create_xmp_sidecars
        RESUME_CACHE.clear()
        MEDIA_ID_CACHE.clear()

    @app.post("/settings")
    def update_settings():
        apply_settings_form(request.form)
        PROGRESS.emit_event(
            "auth.settings.updated",
            f"Auth method set to {current_auth_method}",
            phase="selection",
            auth_method=current_auth_method,
            cli_message=f"Auth method set to {current_auth_method}",
        )
        return redirect(url_for("index"))

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(
            app.static_folder,
            "favicon.ico",
            mimetype="image/vnd.microsoft.icon",
        )

    @app.post("/upload")
    def upload():
        nonlocal current_har_name
        uploaded = request.files.get("har_file")
        if not uploaded or not uploaded.filename:
            PROGRESS.emit_event(
                "error.validation",
                "No HAR file selected",
                level="WARNING",
                phase="selection",
                user_action="Select a HAR file to upload.",
            )
            return redirect(url_for("index"))

        filename = secure_filename(uploaded.filename)
        if not filename.endswith(".har"):
            filename = f"{filename}.har"

        uploaded.save(data_dir / filename)
        current_har_name = filename
        RESUME_CACHE.clear()
        MEDIA_ID_CACHE.clear()
        PROGRESS.emit_event(
            "har.scan.started",
            "HAR file uploaded",
            phase="scan",
            state_label="Uploaded",
            har_file=filename,
            cli_message=f"Uploaded HAR file: {filename}",
        )
        try:
            prepare_runtime_manifest_state(effective_args(), filename)
        except Exception as exc:
            LOGGER.warning(
                "Failed to summarize uploaded HAR file %s: %s",
                filename,
                exc,
            )
            PROGRESS.emit_event(
                "error.validation",
                "Could not summarize uploaded HAR",
                level="WARNING",
                phase="scan",
                har_file=filename,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return redirect(url_for("index"))

    def any_job_running() -> bool:
        return (
            (JOB_THREAD is not None and JOB_THREAD.is_alive())
            or (SIDECAR_THREAD is not None and SIDECAR_THREAD.is_alive())
            or (TELEMETRY_THREAD is not None and TELEMETRY_THREAD.is_alive())
            or (MERGE_THREAD is not None and MERGE_THREAD.is_alive())
        )

    @app.post("/start")
    def start():
        global JOB_THREAD

        apply_settings_form(request.form)
        run_args = effective_args()
        using_api_token = current_auth_method == AUTH_METHOD_API_TOKEN
        form_har = request.form.get("har_file") or current_har_name
        selected_har = None if using_api_token else form_har
        with JOB_LOCK:
            if any_job_running():
                PROGRESS.emit_event(
                    "error.validation",
                    "A job is already running",
                    level="WARNING",
                    phase="selection",
                    user_action="Wait for the current job to finish or stop it.",
                )
                return job_action_response()

            if not using_api_token and not selected_har:
                PROGRESS.emit_event(
                    "error.validation",
                    "No HAR file selected",
                    level="WARNING",
                    phase="selection",
                    user_action="Select a HAR file before starting.",
                )
                return job_action_response()

            try:
                prepared = prepare_runtime_manifest_state(run_args, selected_har)
            except Exception as exc:
                LOGGER.warning("Could not load media for download: %s", exc)
                PROGRESS.emit_event(
                    "error.validation",
                    "Could not load media for download",
                    level="WARNING",
                    phase="selection",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                return job_action_response()

            valid_keys = {item.key for item in prepared.manifest.media}
            if request.form.get("selected_media_mode") == "all_pending":
                selected_keys = pending_keys(prepared.state) & valid_keys
            else:
                selected_keys = {
                    value
                    for value in request.form.getlist("selected_media_keys")
                    if value
                }
            invalid_keys = selected_keys - valid_keys
            if invalid_keys:
                PROGRESS.emit_event(
                    "error.validation",
                    "Selected media no longer matches the HAR",
                    level="WARNING",
                    phase="selection",
                    state_label="Ready",
                    user_action="Refresh and try again.",
                )
                return job_action_response()
            if not selected_keys:
                PROGRESS.emit_event(
                    "media.selection.empty",
                    "No pending media selected",
                    level="WARNING",
                    phase="selection",
                    state_label="Ready",
                    user_action="Select at least one file to start downloading.",
                )
                return job_action_response()
            try:
                files_per_batch = parse_files_per_batch()
            except ValueError as exc:
                PROGRESS.emit_event(
                    "error.validation",
                    str(exc),
                    level="WARNING",
                    phase="selection",
                    state_label="Ready",
                    user_action="Enter a positive files-per-batch value.",
                )
                return job_action_response()

            paths = prepared.paths
            selected_media = [
                item for item in prepared.manifest.media if item.key in selected_keys
            ]
            job_id = uuid4().hex
            completed_count = state_completed_count(prepared.state)
            MEDIA_ID_CACHE.clear()
            PROGRESS.update(
                job_id=job_id,
                status="running",
                state_label="Starting",
                message="Preparing the selected download job.",
                stop_requested=False,
                notifications=[],
                total_ids=len(prepared.manifest.media),
                completed_ids=completed_count,
                pending_ids=max(len(prepared.manifest.media) - completed_count, 0),
                total_batches=0,
                completed_batches=0,
                failed_batches=0,
                current_batch=0,
                current_batch_size=0,
                current_batch_keys=[],
                current_download_bytes=0,
                current_download_total=0,
                current_download_started_at=0,
                current_download_speed_bps=0,
                current_download_elapsed_seconds=0,
                output_dir=str(paths.output_dir),
                sidecar_status=(
                    "pending" if current_create_xmp_sidecars else STATUS_COMPLETE
                ),
                sidecar_count=0,
                sidecar_dir=str(paths.output_dir),
                sidecar_message=(
                    "XMP sidecar generation queued."
                    if current_create_xmp_sidecars
                    else "XMP sidecar generation disabled."
                ),
                har_file=auth_source_label(paths.auth),
            )
            PROGRESS.emit_event(
                "download.plan.created",
                "Download plan created",
                phase="download",
                job_id_guard=job_id,
                set_message=False,
                selected_count=len(selected_media),
                pending_count=len(selected_keys),
                already_downloaded_count=completed_count,
                har_file=auth_source_label(paths.auth),
            )

            # XMP + telemetry generation run inside run_download_job itself,
            # sequenced before the media download (see runtime.py) -- not as
            # a separate concurrent thread, so XMP generation can reliably
            # see GPS data telemetry just fetched instead of racing it.
            JOB_THREAD = threading.Thread(
                target=run_download_job,
                args=(
                    run_args,
                    PROGRESS,
                    selected_har,
                    selected_keys,
                    files_per_batch,
                    job_id,
                ),
                daemon=True,
            )
            JOB_THREAD.start()

        return job_action_response()

    @app.post("/update-sidecars")
    def update_sidecars():
        global SIDECAR_THREAD, TELEMETRY_THREAD

        with JOB_LOCK:
            if any_job_running():
                PROGRESS.emit_event(
                    "error.validation",
                    "A job is already running",
                    level="WARNING",
                    phase="selection",
                    user_action="Wait for the current job to finish or stop it.",
                )
                return job_action_response()

            if not has_active_auth():
                PROGRESS.emit_event(
                    "error.validation",
                    "No HAR file or API token configured",
                    level="WARNING",
                    phase="selection",
                    user_action=(
                        "Configure a HAR file or API token before updating sidecars."
                    ),
                )
                return job_action_response()

            try:
                prepared = prepare_runtime_manifest_state(
                    effective_args(), active_har_arg()
                )
            except Exception as exc:
                PROGRESS.emit_event(
                    "error.validation",
                    "Could not load media for sidecar update",
                    level="WARNING",
                    phase="selection",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                return job_action_response()

            paths = prepared.paths
            downloaded = downloaded_keys(prepared.state)
            media_items = [
                item for item in prepared.manifest.media if item.key in downloaded
            ]
            if not media_items:
                PROGRESS.emit_event(
                    "error.validation",
                    "No downloaded media to update sidecars for",
                    level="WARNING",
                    phase="selection",
                    user_action="Download at least one file before updating sidecars.",
                )
                return job_action_response()

            headers = resolve_headers(paths.auth)
            job_id = uuid4().hex
            MEDIA_ID_CACHE.clear()

            PROGRESS.update(
                job_id=job_id,
                status="running",
                state_label="Updating sidecars",
                stop_requested=False,
                finished_at="",
            )
            PROGRESS.emit_event(
                "sidecar.update.started",
                f"Updating XMP, GPX/GPMF, and mediainfo for {len(media_items)} file(s)",
                level="ACTIVE",
                phase="sidecars",
                job_id_guard=job_id,
                set_message=False,
            )

            SIDECAR_THREAD = None
            TELEMETRY_THREAD = threading.Thread(
                target=run_metadata_update_job,
                args=(
                    headers,
                    media_items,
                    paths.output_dir,
                    paths.har_path,
                    paths.state_file,
                    PROGRESS,
                    job_id,
                ),
                daemon=True,
            )
            TELEMETRY_THREAD.start()

        return job_action_response()

    @app.post("/rerun-merge")
    def rerun_merge():
        global MERGE_THREAD

        with JOB_LOCK:
            if any_job_running():
                PROGRESS.emit_event(
                    "error.validation",
                    "A job is already running",
                    level="WARNING",
                    phase="selection",
                    user_action="Wait for the current job to finish or stop it.",
                )
                return job_action_response()

            if not has_active_auth():
                PROGRESS.emit_event(
                    "error.validation",
                    "No HAR file or API token configured",
                    level="WARNING",
                    phase="selection",
                    user_action=(
                        "Configure a HAR file or API token before re-merging."
                    ),
                )
                return job_action_response()

            try:
                prepared = prepare_runtime_manifest_state(
                    effective_args(), active_har_arg()
                )
            except Exception as exc:
                PROGRESS.emit_event(
                    "error.validation",
                    "Could not load media for re-merge",
                    level="WARNING",
                    phase="selection",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                return job_action_response()

            paths = prepared.paths
            valid_keys = {item.key for item in prepared.manifest.media}
            selected_keys = {
                value
                for value in request.form.getlist("selected_merge_keys")
                if value
            }
            invalid_keys = selected_keys - valid_keys
            if invalid_keys:
                PROGRESS.emit_event(
                    "error.validation",
                    "Selected media no longer matches the HAR",
                    level="WARNING",
                    phase="selection",
                    user_action="Refresh and try again.",
                )
                return job_action_response()

            # Only re-merge rows the dashboard itself would offer the
            # checkbox for -- chaptered items with something recoverable on
            # disk (merged, size_mismatch, or chapters_ready). This mirrors
            # media_sidecar_rows()'s own eligibility check server-side so a
            # stale/forged form submission can't force a re-merge attempt on
            # a row with nothing to merge from.
            chapter_listing = build_chapter_directory_listing(paths.output_dir)
            media_items = [
                item
                for item in prepared.manifest.media
                if item.key in selected_keys
                and compute_merge_status(
                    item,
                    media_download_path(paths.output_dir, item.filename),
                    chapter_listing,
                )
                in MERGE_STATUS_REMERGE_ELIGIBLE
            ]
            if not media_items:
                PROGRESS.emit_event(
                    "error.validation",
                    "No eligible chaptered media selected for re-merge",
                    level="WARNING",
                    phase="selection",
                    user_action="Select at least one chaptered row to re-merge.",
                )
                return job_action_response()

            job_id = uuid4().hex
            MEDIA_ID_CACHE.clear()
            PROGRESS.update(
                job_id=job_id,
                status="running",
                state_label="Re-merging",
                stop_requested=False,
                finished_at="",
            )
            PROGRESS.emit_event(
                "download.chapter.remerge_started",
                f"Re-merging {len(media_items)} chaptered file(s)",
                level="ACTIVE",
                phase="download",
                job_id_guard=job_id,
                set_message=False,
            )

            MERGE_THREAD = threading.Thread(
                target=run_remerge_job,
                args=(
                    media_items,
                    paths.output_dir,
                    paths.state_file,
                    PROGRESS,
                    job_id,
                ),
                daemon=True,
            )
            MERGE_THREAD.start()

        return job_action_response()

    @app.post("/stop")
    def stop():
        if JOB_THREAD and JOB_THREAD.is_alive():
            PROGRESS.update(stop_requested=True, state_label="Stopping")
            PROGRESS.emit_event(
                "run.stop_requested",
                "Stop requested by user",
                level="WARNING",
                phase="cleanup",
                state_label="Stopping",
                next_action="cleanup",
            )
        else:
            PROGRESS.emit_event(
                "error.validation",
                "No active download job to stop",
                level="WARNING",
                phase="cleanup",
                state_label=PROGRESS.state_label,
            )
        return job_action_response()

    @app.get("/status")
    def status():
        return jsonify(PROGRESS.snapshot())

    @app.get("/api/runs/current/events")
    def current_events():
        return jsonify({"items": recent_events(PROGRESS.job_id)})

    @app.get("/api/runs/current/status")
    def current_status():
        return jsonify(current_run_status() or PROGRESS.snapshot())

    @app.get("/sidecars")
    def sidecars():
        return jsonify({"items": media_sidecar_rows()})

    refresh_resume_counts(log_summary=True)
    return app
