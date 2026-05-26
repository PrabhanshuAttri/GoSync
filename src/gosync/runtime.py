import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gosync.config import resolve_inside_data_dir
from gosync.constants import (
    DEFAULT_MANIFEST_FILE,
    DEFAULT_MEDIA_RESPONSES_FILE,
    STATUS_COMPLETE,
    STATUS_STOPPED,
)
from gosync.downloader import (
    DownloadCancelled,
    completed_count_for_items,
    extract_browser_headers,
    process_pipeline,
    resolve_har_file,
)
from gosync.events import log_event, new_run_id
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.manifest import (
    MediaManifest,
    extension_counts,
    format_extension_summary,
    read_manifest_from_har,
    write_manifest,
    write_media_responses_dump,
)
from gosync.progress import ProgressState
from gosync.report import build_run_summary, build_run_summary_event, write_run_report
from gosync.sidecar import run_sidecar_job
from gosync.state import (
    create_or_update_state,
    downloaded_extension_counts,
    format_downloaded_extension_summary,
    load_state,
    sync_state_with_downloads,
)


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    har_path: Path
    output_dir: Path
    state_file: Path
    manifest_file: Path
    media_dump_file: Path


@dataclass(frozen=True)
class PreparedManifestState:
    paths: RuntimePaths
    manifest: MediaManifest
    state: dict
    sync_changes: list[dict[str, str]]


def get_runtime_paths(
    args: argparse.Namespace,
    har_file: str | None = None,
) -> RuntimePaths:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    return RuntimePaths(
        data_dir,
        resolve_har_file(data_dir, har_file or args.har_file),
        resolve_inside_data_dir(data_dir, args.output_folder).resolve(),
        resolve_inside_data_dir(data_dir, args.state_file).resolve(),
        resolve_inside_data_dir(data_dir, DEFAULT_MANIFEST_FILE).resolve(),
        resolve_inside_data_dir(data_dir, DEFAULT_MEDIA_RESPONSES_FILE).resolve(),
    )


def runtime_cache_key(paths: RuntimePaths) -> tuple[str, float, str, float, str, float]:
    return (
        str(paths.har_path),
        paths.har_path.stat().st_mtime,
        str(paths.state_file),
        paths.state_file.stat().st_mtime if paths.state_file.exists() else 0,
        str(paths.output_dir),
        paths.output_dir.stat().st_mtime if paths.output_dir.exists() else 0,
    )


def prepare_manifest_state(
    har_path: Path,
    output_dir: Path,
    state_file: Path,
    manifest_file: Path,
    media_dump_file: Path,
    progress: ProgressState | None = None,
) -> tuple[MediaManifest, dict, list[dict[str, str]]]:
    manifest = read_manifest_from_har(har_path)
    write_manifest(manifest, manifest_file, har_path)
    write_media_responses_dump(manifest, media_dump_file, har_path)

    create_or_update_state(state_file, manifest)
    state, sync_changes = sync_state_with_downloads(state_file, output_dir)
    if progress:
        counts = {
            key.upper(): value for key, value in extension_counts(manifest).items()
        }
        progress.emit_event(
            "media.scan.completed",
            "Media scan completed",
            phase="scan",
            job_id_guard=progress.job_id,
            set_message=False,
            counts_by_extension=counts,
            total_count=len(manifest.media),
        )
        downloaded_counts = {
            key.upper(): value
            for key, value in downloaded_extension_counts(state).items()
        }
        progress.emit_event(
            "media.scan.completed",
            "Already downloaded files detected",
            phase="scan",
            job_id_guard=progress.job_id,
            set_message=False,
            already_downloaded_count=sum(downloaded_counts.values()),
            already_downloaded_by_extension=downloaded_counts,
            cli_message=(
                "Already downloaded: "
                f"{sum(downloaded_counts.values()):,} files"
                + (
                    ", "
                    + ", ".join(
                        f"{extension} {count:,}"
                        for extension, count in downloaded_counts.items()
                    )
                    if downloaded_counts
                    else ""
                )
            ),
        )
        for duplicate in manifest.duplicates:
            progress.emit_event(
                "download.file.skipped",
                "Duplicate media entry skipped",
                level="WARNING",
                phase="scan",
                job_id_guard=progress.job_id,
                set_message=False,
                file_name=duplicate.filename,
                file_id=duplicate.media_id,
            )
        for change in sync_changes:
            progress.emit_event(
                "media.scan.completed",
                "Resume state synchronized",
                phase="scan",
                job_id_guard=progress.job_id,
                set_message=False,
                file_name=change["filename"],
                file_id=change["id"],
                status=change["status"],
            )

    return manifest, state, sync_changes


def prepare_runtime_manifest_state(
    args: argparse.Namespace,
    har_file: str | None = None,
    progress: ProgressState | None = None,
) -> PreparedManifestState:
    paths = get_runtime_paths(args, har_file)
    return prepare_paths_manifest_state(paths, progress)


def prepare_paths_manifest_state(
    paths: RuntimePaths,
    progress: ProgressState | None = None,
) -> PreparedManifestState:
    manifest, state, sync_changes = prepare_manifest_state(
        paths.har_path,
        paths.output_dir,
        paths.state_file,
        paths.manifest_file,
        paths.media_dump_file,
        progress,
    )
    return PreparedManifestState(paths, manifest, state, sync_changes)


def format_manifest_state_summary(manifest: MediaManifest, state: dict) -> str:
    return "\n".join(
        [
            format_extension_summary(manifest),
            format_downloaded_extension_summary(state),
        ]
    )


def run_once(args: argparse.Namespace) -> int:
    prepared = prepare_runtime_manifest_state(args)
    paths = prepared.paths
    log_file = configure_file_logging(paths.data_dir)

    run_id = new_run_id()
    log_event(
        "app.starting",
        "GoSync run started",
        run_id=run_id,
        data_dir=str(paths.data_dir),
        har_file=paths.har_path.name,
        destination=str(paths.output_dir),
        state_file=str(paths.state_file),
        batch_max_bytes=args.batch_max_bytes,
        cli_message=f"GoSync run started with HAR file {paths.har_path.name}",
    )
    LOGGER.info("File logging enabled at %s", log_file)
    LOGGER.info(
        "Starting run-once download. data_dir=%s har_file=%s output_dir=%s "
        "state_file=%s batch_max_bytes=%s",
        paths.data_dir,
        paths.har_path,
        paths.output_dir,
        paths.state_file,
        args.batch_max_bytes,
    )

    manifest = prepared.manifest
    log_event(
        "media.scan.completed",
        "Media scan completed",
        level="INFO",
        phase="scan",
        run_id=run_id,
        counts_by_extension={
            key.upper(): value
            for key, value in extension_counts(prepared.manifest).items()
        },
        total_count=len(prepared.manifest.media),
    )
    headers = extract_browser_headers(paths.har_path)
    try:
        run_sidecar_job(
            paths.har_path,
            paths.output_dir,
            media_items=manifest.media,
            state_file=paths.state_file,
        )
    except Exception:
        LOGGER.exception("Sidecar generation failed; continuing download.")
    process_pipeline(
        media_items=manifest.media,
        data_dir=paths.data_dir,
        output_dir=paths.output_dir,
        state_file=paths.state_file,
        headers=headers,
        batch_max_bytes=args.batch_max_bytes,
    )
    final_state = load_state(paths.state_file)
    report_path = write_run_report(
        paths.data_dir,
        final_state,
        manifest,
        STATUS_COMPLETE,
        prepared.sync_changes,
    )
    log_event(
        "run.cleanup.completed",
        "Run completed",
        phase="cleanup",
        run_id=run_id,
        destination=str(paths.output_dir),
        report_path=str(report_path),
        cli_message=build_run_summary(
            final_state,
            manifest,
            STATUS_COMPLETE,
            prepared.sync_changes,
            report_path,
        ),
    )
    LOGGER.info("Run-once download completed.")
    return 0


def run_download_job(
    args: argparse.Namespace,
    progress: ProgressState,
    har_file: str | None = None,
    selected_keys: set[str] | None = None,
    batch_file_limit: str | int | None = None,
    job_id: str | None = None,
) -> None:
    manifest: MediaManifest | None = None
    sync_changes: list[dict[str, str]] = []
    data_dir: Path | None = None
    state_file: Path | None = None
    active_job_id = job_id or ""
    try:
        active_job_id = active_job_id or new_run_id()
        paths = get_runtime_paths(args, har_file)
        data_dir = paths.data_dir
        state_file = paths.state_file
        progress.update(
            status="running",
            state_label="Starting",
            job_id=active_job_id,
            stop_requested=False,
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at="",
            report_path="",
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
            sidecar_dir=str(paths.output_dir),
            har_file=str(paths.har_path),
        )
        progress.emit_event(
            "har.scan.started",
            "Scanning HAR file",
            phase="auth",
            job_id_guard=active_job_id,
            har_file=paths.har_path.name,
        )
        prepared = prepare_paths_manifest_state(paths)
        manifest = prepared.manifest
        progress.emit_event(
            "media.scan.completed",
            "Media scan completed",
            phase="scan",
            job_id_guard=active_job_id,
            set_message=False,
            counts_by_extension={
                key.upper(): value for key, value in extension_counts(manifest).items()
            },
            total_count=len(manifest.media),
        )
        media_items = (
            [item for item in manifest.media if item.key in selected_keys]
            if selected_keys is not None
            else manifest.media
        )
        state = prepared.state
        sync_changes = prepared.sync_changes
        completed_count = completed_count_for_items(state, manifest.media)
        progress.update(
            job_id_guard=active_job_id,
            total_ids=len(manifest.media),
            completed_ids=completed_count,
            pending_ids=max(len(manifest.media) - completed_count, 0),
            sidecar_status="pending",
            sidecar_count=0,
            sidecar_dir=str(paths.output_dir),
            sidecar_message="XMP sidecar generation queued.",
        )
        if selected_keys is not None:
            progress.emit_event(
                "media.selection.completed",
                "Media selection completed",
                phase="selection",
                job_id_guard=active_job_id,
                selected_count=len(media_items),
                cli_message=f"Selected {len(media_items):,} media files for download.",
            )
        headers = extract_browser_headers(paths.har_path, progress, active_job_id)
        process_pipeline(
            media_items=media_items,
            data_dir=paths.data_dir,
            output_dir=paths.output_dir,
            state_file=paths.state_file,
            headers=headers,
            batch_max_bytes=args.batch_max_bytes,
            progress=progress,
            batch_file_limit=batch_file_limit,
            batch_cap_media_items=manifest.media,
            progress_media_items=manifest.media,
            job_id=active_job_id,
        )
        final_state = load_state(state_file)
        report_path = write_run_report(
            data_dir,
            final_state,
            manifest,
            STATUS_COMPLETE,
            sync_changes,
        )
        progress.update(
            job_id_guard=active_job_id,
            status="complete",
            state_label="Completed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            current_batch=0,
            current_batch_size=0,
            current_batch_keys=[],
            current_download_bytes=0,
            current_download_total=0,
            report_path=str(report_path),
        )
        summary_title, summary_details = build_run_summary_event(
            final_state,
            manifest,
            STATUS_COMPLETE,
            sync_changes,
            report_path,
        )
        progress.emit_event(
            "run.cleanup.completed",
            summary_title,
            level="SUCCESS",
            phase="cleanup",
            title=summary_title,
            details=summary_details,
            job_id_guard=active_job_id,
            set_message=False,
            summary=summary_title,
            report_path=str(report_path),
        )
    except DownloadCancelled:
        report_path = ""
        final_state = None
        if data_dir and state_file and manifest:
            final_state = load_state(state_file)
            report_path = str(
                write_run_report(
                    data_dir,
                    final_state,
                    manifest,
                    STATUS_STOPPED,
                    sync_changes,
                )
            )
        progress.update(
            job_id_guard=active_job_id,
            status="stopped",
            state_label="Stopped",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            current_batch=0,
            current_batch_size=0,
            current_batch_keys=[],
            current_download_bytes=0,
            current_download_total=0,
            current_download_started_at=0,
            current_download_speed_bps=0,
            current_download_elapsed_seconds=0,
            stop_requested=False,
            report_path=report_path,
        )
        if final_state and manifest:
            summary_title, summary_details = build_run_summary_event(
                final_state,
                manifest,
                STATUS_STOPPED,
                sync_changes,
                report_path,
            )
            progress.emit_event(
                "run.stopped",
                summary_title,
                level="WARNING",
                phase="cleanup",
                title=summary_title,
                details=summary_details,
                job_id_guard=active_job_id,
                set_message=False,
                summary=summary_title,
                report_path=report_path,
            )
        else:
            progress.emit_event(
                "run.stopped",
                "Run stopped safely",
                phase="cleanup",
                state_label="Stopped",
                job_id_guard=active_job_id,
                status="stopped",
            )
    except Exception as exc:
        progress.update(
            job_id_guard=active_job_id,
            status="failed",
            state_label="Failed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            stop_requested=False,
        )
        LOGGER.exception("Download job failed.")
        progress.emit_event(
            "error.unhandled",
            "Unhandled download error",
            level="ERROR",
            phase="download",
            job_id_guard=active_job_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            state_label="Failed",
            cli_message=f"Download failed: {exc}",
        )
