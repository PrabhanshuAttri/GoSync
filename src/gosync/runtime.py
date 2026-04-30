import argparse
from datetime import datetime
from pathlib import Path

from gosync.config import resolve_inside_data_dir
from gosync.downloader import (
    DownloadCancelled,
    extract_browser_headers,
    extract_ids,
    process_pipeline,
    resolve_har_file,
)
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.progress import ProgressState


def get_runtime_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")

    har_path = resolve_har_file(data_dir, args.har_file)
    output_dir = resolve_inside_data_dir(data_dir, args.output_folder).resolve()
    completed_log = resolve_inside_data_dir(data_dir, args.completed_log).resolve()
    return data_dir, har_path, output_dir, completed_log


def run_once(args: argparse.Namespace) -> int:
    data_dir, har_path, output_dir, completed_log = get_runtime_paths(args)
    log_file = configure_file_logging(data_dir)

    print("========================================", flush=True)
    print("             GoSync Utility             ", flush=True)
    print("========================================", flush=True)
    print(f"Data directory: {data_dir}", flush=True)
    print(f"HAR file: {har_path}", flush=True)
    print(f"Output folder: {output_dir}", flush=True)
    print(f"Completed log: {completed_log}", flush=True)
    print(f"Batch size: {args.batch_size}", flush=True)
    LOGGER.info("File logging enabled at %s", log_file)
    LOGGER.info(
        "Starting run-once download. data_dir=%s har_file=%s output_dir=%s completed_log=%s batch_size=%s",
        data_dir,
        har_path,
        output_dir,
        completed_log,
        args.batch_size,
    )

    ids = extract_ids(har_path)
    headers = extract_browser_headers(har_path)
    process_pipeline(
        all_ids=ids,
        data_dir=data_dir,
        output_dir=output_dir,
        completed_log=completed_log,
        headers=headers,
        batch_size=args.batch_size,
        max_retry_passes=args.max_retry_passes,
    )
    LOGGER.info("Run-once download completed.")
    return 0


def run_download_job(
    args: argparse.Namespace,
    progress: ProgressState,
    har_file: str | None = None,
) -> None:
    if har_file:
        args.har_file = har_file

    try:
        data_dir, har_path, output_dir, completed_log = get_runtime_paths(args)
        progress.update(
            status="running",
            state_label="Processing",
            stop_requested=False,
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at="",
            total_ids=0,
            completed_ids=0,
            pending_ids=0,
            total_batches=0,
            completed_batches=0,
            failed_batches=0,
            current_batch=0,
            current_batch_size=0,
            current_download_bytes=0,
            current_download_total=0,
            current_download_started_at=0,
            current_download_speed_bps=0,
            current_download_elapsed_seconds=0,
            output_dir=str(output_dir),
            har_file=str(har_path),
        )
        progress.log(f"Scanning HAR file: {har_path.name}")
        ids = extract_ids(har_path)
        headers = extract_browser_headers(har_path)
        process_pipeline(
            all_ids=ids,
            data_dir=data_dir,
            output_dir=output_dir,
            completed_log=completed_log,
            headers=headers,
            batch_size=args.batch_size,
            max_retry_passes=args.max_retry_passes,
            progress=progress,
        )
        progress.update(
            status="complete",
            state_label="Completed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            current_batch=0,
            current_batch_size=0,
            current_download_bytes=0,
            current_download_total=0,
        )
        progress.log(f"Done. Recovered media is in {output_dir}.")
    except DownloadCancelled:
        progress.update(
            status="stopped",
            state_label="Stopped",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            current_batch=0,
            current_batch_size=0,
            current_download_bytes=0,
            current_download_total=0,
            current_download_started_at=0,
            current_download_speed_bps=0,
            current_download_elapsed_seconds=0,
            stop_requested=False,
        )
        progress.log("Download stopped by user.")
    except Exception as exc:
        progress.update(
            status="failed",
            state_label="Failed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            stop_requested=False,
        )
        LOGGER.exception("Download job failed.")
        progress.log(f"Failed: {exc}")
