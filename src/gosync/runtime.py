import argparse
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from gosync.config import resolve_inside_data_dir
from gosync.constants import (
    DEFAULT_MANIFEST_FILE,
    DEFAULT_MEDIA_RESPONSES_FILE,
    STATUS_COMPLETE,
    STATUS_STOPPED,
)
from gosync.downloader import (
    DownloadCancelled,
    extract_browser_headers,
    process_pipeline,
    resolve_har_file,
)
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.manifest import (
    MediaManifest,
    format_extension_summary,
    read_manifest_from_har,
    write_manifest,
    write_media_responses_dump,
)
from gosync.progress import ProgressState
from gosync.report import build_run_summary, write_run_report
from gosync.sidecar import run_sidecar_job
from gosync.state import (
    completed_count,
    create_or_update_state,
    format_downloaded_extension_summary,
    load_state,
    sync_state_with_downloads,
)


def get_runtime_paths(
    args: argparse.Namespace,
    har_file: str | None = None,
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    har_path = resolve_har_file(data_dir, har_file or args.har_file)
    output_dir = resolve_inside_data_dir(data_dir, args.output_folder).resolve()
    sidecar_dir = resolve_inside_data_dir(data_dir, args.sidecar_folder).resolve()
    state_file = resolve_inside_data_dir(data_dir, args.state_file).resolve()
    manifest_file = resolve_inside_data_dir(data_dir, DEFAULT_MANIFEST_FILE).resolve()
    media_dump_file = resolve_inside_data_dir(
        data_dir,
        DEFAULT_MEDIA_RESPONSES_FILE,
    ).resolve()
    return (
        data_dir,
        har_path,
        output_dir,
        sidecar_dir,
        state_file,
        manifest_file,
        media_dump_file,
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

    if progress:
        progress.log_background(format_extension_summary(manifest))
        for duplicate in manifest.duplicates:
            progress.log_background(
                "Skipped duplicate media entry: "
                f"{duplicate.filename} ({duplicate.media_id})"
            )

    create_or_update_state(state_file, manifest)
    state, sync_changes = sync_state_with_downloads(state_file, output_dir)
    if progress:
        for change in sync_changes:
            progress.log_background(
                "Resume sync: "
                f"{change['filename']} ({change['id']}) marked {change['status']}."
            )

    return manifest, state, sync_changes


def startup_media_summaries(args: argparse.Namespace) -> list[str]:
    try:
        (
            _data_dir,
            har_path,
            output_dir,
            _sidecar_dir,
            state_file,
            manifest_file,
            media_dump_file,
        ) = get_runtime_paths(args)
        manifest, state, _sync_changes = prepare_manifest_state(
            har_path,
            output_dir,
            state_file,
            manifest_file,
            media_dump_file,
        )
    except Exception:
        return []
    return [
        format_extension_summary(manifest),
        format_downloaded_extension_summary(state),
    ]


def run_once(args: argparse.Namespace) -> int:
    (
        data_dir,
        har_path,
        output_dir,
        sidecar_dir,
        state_file,
        manifest_file,
        media_dump_file,
    ) = get_runtime_paths(args)
    log_file = configure_file_logging(data_dir)

    print("========================================", flush=True)
    print("             GoSync Utility             ", flush=True)
    print("========================================", flush=True)
    print(f"Data directory: {data_dir}", flush=True)
    print(f"HAR file: {har_path}", flush=True)
    print(f"Output folder: {output_dir}", flush=True)
    print(f"Sidecars: next to media files in {output_dir}", flush=True)
    print(f"State file: {state_file}", flush=True)
    print(f"Batch max bytes: {args.batch_max_bytes}", flush=True)
    LOGGER.info("File logging enabled at %s", log_file)
    LOGGER.info(
        "Starting run-once download. data_dir=%s har_file=%s output_dir=%s "
        "state_file=%s batch_max_bytes=%s",
        data_dir,
        har_path,
        output_dir,
        state_file,
        args.batch_max_bytes,
    )

    manifest, state, sync_changes = prepare_manifest_state(
        har_path,
        output_dir,
        state_file,
        manifest_file,
        media_dump_file,
    )
    print(format_extension_summary(manifest), flush=True)
    print(format_downloaded_extension_summary(state), flush=True)
    headers = extract_browser_headers(har_path)
    try:
        run_sidecar_job(
            har_path,
            output_dir,
            media_items=manifest.media,
            state_file=state_file,
        )
    except Exception:
        LOGGER.exception("Sidecar generation failed; continuing download.")
    process_pipeline(
        media_items=manifest.media,
        data_dir=data_dir,
        output_dir=output_dir,
        state_file=state_file,
        headers=headers,
        batch_max_bytes=args.batch_max_bytes,
    )
    final_state = load_state(state_file)
    report_path = write_run_report(
        data_dir,
        final_state,
        manifest,
        STATUS_COMPLETE,
        sync_changes,
    )
    print(
        build_run_summary(
            final_state,
            manifest,
            STATUS_COMPLETE,
            sync_changes,
            report_path,
        ),
        flush=True,
    )
    print(f"Run report: {report_path}", flush=True)
    LOGGER.info("Run-once download completed.")
    return 0


def run_download_job(
    args: argparse.Namespace,
    progress: ProgressState,
    har_file: str | None = None,
) -> None:
    manifest: MediaManifest | None = None
    sync_changes: list[dict[str, str]] = []
    data_dir: Path | None = None
    state_file: Path | None = None
    try:
        job_id = uuid4().hex
        (
            data_dir,
            har_path,
            output_dir,
            sidecar_dir,
            state_file,
            manifest_file,
            media_dump_file,
        ) = get_runtime_paths(args, har_file)
        progress.update(
            status="running",
            state_label="Processing",
            job_id=job_id,
            stop_requested=False,
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at="",
            report_path="",
            total_ids=0,
            completed_ids=0,
            pending_ids=0,
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
            output_dir=str(output_dir),
            sidecar_dir=str(output_dir),
            har_file=str(har_path),
        )
        progress.log(f"Scanning HAR file: {har_path.name}")
        manifest, state, sync_changes = prepare_manifest_state(
            har_path,
            output_dir,
            state_file,
            manifest_file,
            media_dump_file,
            progress,
        )
        progress.update(
            total_ids=len(manifest.media),
            completed_ids=completed_count(state),
            pending_ids=max(len(manifest.media) - completed_count(state), 0),
            sidecar_status="pending",
            sidecar_count=0,
            sidecar_dir=str(output_dir),
            sidecar_message="XMP sidecar generation queued.",
        )
        headers = extract_browser_headers(har_path)
        process_pipeline(
            media_items=manifest.media,
            data_dir=data_dir,
            output_dir=output_dir,
            state_file=state_file,
            headers=headers,
            batch_max_bytes=args.batch_max_bytes,
            progress=progress,
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
        progress.log_background(
            build_run_summary(
                final_state,
                manifest,
                STATUS_COMPLETE,
                sync_changes,
                report_path,
            )
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
            progress.log_background(
                build_run_summary(
                    final_state,
                    manifest,
                    STATUS_STOPPED,
                    sync_changes,
                    report_path,
                )
            )
        else:
            progress.log_event("Download stopped by user.", "Stopped")
    except Exception as exc:
        progress.update(
            status="failed",
            state_label="Failed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            stop_requested=False,
        )
        LOGGER.exception("Download job failed.")
        progress.log(f"Failed: {exc}")
