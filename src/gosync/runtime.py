import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gosync.auth import AuthConfig, build_api_token_headers, resolve_auth_config
from gosync.config import resolve_inside_data_dir
from gosync.constants import (
    AUTH_METHOD_API_TOKEN,
    DEFAULT_MANIFEST_FILE,
    DEFAULT_MEDIA_RESPONSES_FILE,
    STATUS_COMPLETE,
    STATUS_STOPPED,
)
from gosync.downloader import (
    DownloadCancelled,
    completed_count_for_items,
    extract_browser_headers,
    media_event_payload,
    merge_chapter_files,
    process_pipeline,
)
from gosync.events import log_event, new_run_id
from gosync.logging_config import LOGGER, configure_file_logging
from gosync.manifest import (
    MediaItem,
    MediaManifest,
    extension_counts,
    format_extension_summary,
    read_manifest_from_api,
    read_manifest_from_har,
    write_manifest,
    write_media_responses_dump,
)
from gosync.paths import media_download_path, safe_child_path
from gosync.progress import ProgressState
from gosync.report import build_run_summary, build_run_summary_event, write_run_report
from gosync.sidecar import run_sidecar_job
from gosync.state import (
    create_or_update_state,
    downloaded_extension_counts,
    format_downloaded_extension_summary,
    load_state,
    mark_downloaded,
    refresh_file_sizes,
    sync_state_with_downloads,
)
from gosync.telemetry import run_telemetry_job


@dataclass(frozen=True)
class RuntimePaths:
    data_dir: Path
    har_path: Path | None
    output_dir: Path
    state_file: Path
    manifest_file: Path
    media_dump_file: Path
    auth: AuthConfig


@dataclass(frozen=True)
class PreparedManifestState:
    paths: RuntimePaths
    manifest: MediaManifest
    state: dict
    sync_changes: list[dict[str, str]]


def auth_source_label(auth: AuthConfig) -> str:
    if auth.method == AUTH_METHOD_API_TOKEN:
        return "API token"
    return auth.har_path.name if auth.har_path else "HAR file"


def resolve_headers(
    auth: AuthConfig,
    progress: ProgressState | None = None,
    job_id: str | None = None,
) -> dict[str, str]:
    if auth.method == AUTH_METHOD_API_TOKEN:
        return build_api_token_headers(auth.auth_token)
    return extract_browser_headers(auth.har_path, progress, job_id)


def get_runtime_paths(
    args: argparse.Namespace,
    har_file: str | None = None,
) -> RuntimePaths:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    auth = resolve_auth_config(args, data_dir, har_file)

    return RuntimePaths(
        data_dir,
        auth.har_path,
        resolve_inside_data_dir(data_dir, args.output_folder).resolve(),
        resolve_inside_data_dir(data_dir, args.state_file).resolve(),
        resolve_inside_data_dir(data_dir, DEFAULT_MANIFEST_FILE).resolve(),
        resolve_inside_data_dir(data_dir, DEFAULT_MEDIA_RESPONSES_FILE).resolve(),
        auth,
    )


def runtime_cache_key(
    paths: RuntimePaths,
) -> tuple[object, ...]:
    def revision(path: Path | None) -> tuple[int, int, int]:
        if path is None:
            return (0, 0, 0)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return (0, 0, 0)
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    return (
        str(paths.har_path) if paths.har_path else "",
        revision(paths.har_path),
        str(paths.state_file),
        revision(paths.state_file),
        str(paths.output_dir),
        revision(paths.output_dir),
        f"{paths.auth.method}:{paths.auth.auth_token or ''}:{paths.auth.user_id or ''}",
    )


def prepare_manifest_state(
    auth: AuthConfig,
    output_dir: Path,
    state_file: Path,
    manifest_file: Path,
    media_dump_file: Path,
    progress: ProgressState | None = None,
) -> tuple[MediaManifest, dict, list[dict[str, str]]]:
    if auth.method == AUTH_METHOD_API_TOKEN:
        headers = build_api_token_headers(auth.auth_token)
        manifest = read_manifest_from_api(headers, auth.user_id)
    else:
        manifest = read_manifest_from_har(auth.har_path)
    source_label = auth_source_label(auth)
    write_manifest(manifest, manifest_file, source_label)
    write_media_responses_dump(manifest, media_dump_file, source_label)

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
        paths.auth,
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

    auth_label = auth_source_label(paths.auth)
    run_id = new_run_id()
    log_event(
        "app.starting",
        "GoSync run started",
        run_id=run_id,
        data_dir=str(paths.data_dir),
        har_file=auth_label,
        destination=str(paths.output_dir),
        state_file=str(paths.state_file),
        batch_max_bytes=args.batch_max_bytes,
        cli_message=f"GoSync run started with {auth_label}",
    )
    LOGGER.info("File logging enabled at %s", log_file)
    LOGGER.info(
        "Starting run-once download. data_dir=%s auth_source=%s output_dir=%s "
        "state_file=%s batch_max_bytes=%s",
        paths.data_dir,
        auth_label,
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
    headers = resolve_headers(paths.auth)
    # Fetch all metadata/sidecar files (telemetry, then XMP) before the media
    # itself, so they're available even if the media download is slow or
    # interrupted -- and so XMP generation can pick up GPS from the
    # just-fetched telemetry in the same run.
    if getattr(args, "download_telemetry", False):
        try:
            run_telemetry_job(
                headers,
                manifest.media,
                paths.output_dir,
                state_file=paths.state_file,
            )
        except Exception:
            LOGGER.exception("Telemetry download failed; continuing.")
    if getattr(args, "create_xmp_sidecars", True):
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
        auth_label = auth_source_label(paths.auth)
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
            har_file=auth_label,
        )
        progress.emit_event(
            "har.scan.started",
            (
                "Scanning HAR file"
                if paths.auth.method != AUTH_METHOD_API_TOKEN
                else "Fetching media list from GoPro API"
            ),
            phase="auth",
            job_id_guard=active_job_id,
            har_file=auth_label,
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
        headers = resolve_headers(paths.auth, progress, active_job_id)
        # Fetch metadata/telemetry files before the media itself, so they're
        # available even if the media download is slow or interrupted.
        if getattr(args, "download_telemetry", False):
            progress.emit_event(
                "telemetry.generation.started",
                "Fetching mediainfo and telemetry sidecars",
                phase="telemetry",
                job_id_guard=active_job_id,
                set_message=False,
            )
            written, failed = run_telemetry_job(
                headers,
                media_items,
                paths.output_dir,
                state_file=state_file,
                progress=progress,
                job_id=active_job_id,
            )
            progress.emit_event(
                "telemetry.generation.completed",
                f"Telemetry fetched for {written} item(s), {failed} failed.",
                phase="telemetry",
                job_id_guard=active_job_id,
                set_message=False,
                written_count=written,
                failed_count=failed,
            )
        # XMP generation runs after telemetry (not as a concurrent thread) so
        # it can reliably pick up the GPS data telemetry just fetched --
        # a separate thread racing telemetry could read mediainfo.json
        # before telemetry had written it.
        if getattr(args, "create_xmp_sidecars", True):
            try:
                run_sidecar_job(
                    paths.har_path,
                    paths.output_dir,
                    progress=progress,
                    media_items=media_items,
                    state_file=state_file,
                    job_id=active_job_id,
                )
            except Exception:
                LOGGER.exception("Sidecar generation failed; continuing download.")
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


def run_metadata_update_job(
    headers: dict[str, str],
    media_items,
    output_dir: Path,
    har_path: Path | None,
    state_file: Path,
    progress: ProgressState | None = None,
    job_id: str | None = None,
) -> None:
    """Manual "update all sidecars" action: force-refetch telemetry, then
    regenerate XMP sidecars from it -- sequentially in one thread, not two
    concurrent ones, so XMP generation reliably sees the GPS data telemetry
    just fetched instead of racing it."""
    try:
        written, failed = run_telemetry_job(
            headers,
            media_items,
            output_dir,
            state_file=state_file,
            progress=progress,
            job_id=job_id,
            force=True,
        )
        if progress:
            progress.emit_event(
                "telemetry.generation.completed",
                f"Telemetry fetched for {written} item(s), {failed} failed.",
                level="WARNING" if failed else "SUCCESS",
                phase="telemetry",
                job_id_guard=job_id,
                set_message=False,
                written_count=written,
                failed_count=failed,
            )
        run_sidecar_job(
            har_path,
            output_dir,
            progress=progress,
            media_items=media_items,
            state_file=state_file,
            job_id=job_id,
        )
    except Exception as exc:
        LOGGER.exception("Sidecar update job failed.")
        if progress:
            progress.update(
                job_id_guard=job_id,
                status="failed",
                state_label="Failed",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            progress.emit_event(
                "error.unhandled",
                "Unhandled sidecar update error",
                level="ERROR",
                phase="sidecars",
                job_id_guard=job_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                state_label="Failed",
                cli_message=f"Sidecar update failed: {exc}",
            )
        return

    if progress:
        progress.update(
            job_id_guard=job_id,
            status="complete",
            state_label="Completed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


def run_remerge_job(
    media_items: list[MediaItem],
    output_dir: Path,
    state_file: Path,
    progress: ProgressState | None = None,
    job_id: str | None = None,
    on_complete: Callable[[], None] | None = None,
) -> None:
    """User-triggered "Re-run merge" dashboard action: unconditionally
    re-merges the given chaptered items from their local chapter files,
    bypassing the local-reuse "already exists, trust it" shortcut in
    process_pipeline -- the user explicitly asked to redo it (e.g. to pick
    up a fixed exiftool binary and refresh metadata). On failure for a
    given item, per the "no quarantine" decision, its local chapter files
    are left untouched and its download_status simply stays whatever it
    already was -- the normal local-merge short-circuit will retry it from
    the same files on the next regular download trigger."""
    try:
        remerged_keys: list[str] = []
        for item in media_items:
            target_path = media_download_path(output_dir, item.filename)
            if not safe_child_path(output_dir, target_path):
                continue
            if progress:
                progress.emit_event(
                    "download.chapter.remerge_started",
                    f"Re-merging {item.filename}",
                    level="ACTIVE",
                    state_label="Re-merging",
                    job_id_guard=job_id,
                    **media_event_payload(item),
                )
            if merge_chapter_files(output_dir, item, target_path, progress, job_id):
                remerged_keys.append(item.key)
                if progress:
                    progress.emit_event(
                        "download.chapter.remerged",
                        f"Re-merged {item.filename}",
                        level="SUCCESS",
                        job_id_guard=job_id,
                        **media_event_payload(item),
                    )
        if remerged_keys:
            mark_downloaded(state_file, remerged_keys)
            refresh_file_sizes(state_file, output_dir, remerged_keys)
            if progress:
                final_state = load_state(state_file)
                media = final_state.get("media", {})
                total_ids = len(media) if isinstance(media, dict) else 0
                completed_ids = sum(
                    1
                    for record in media.values()
                    if isinstance(record, dict)
                    and record.get("download_status") == "downloaded"
                ) if isinstance(media, dict) else 0
                progress.update(
                    job_id_guard=job_id,
                    total_ids=total_ids,
                    completed_ids=completed_ids,
                    pending_ids=max(total_ids - completed_ids, 0),
                )
    except Exception as exc:
        LOGGER.exception("Re-merge job failed.")
        if progress:
            progress.update(
                job_id_guard=job_id,
                status="failed",
                state_label="Failed",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            progress.emit_event(
                "error.unhandled",
                "Unhandled re-merge error",
                level="ERROR",
                phase="download",
                job_id_guard=job_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                state_label="Failed",
                cli_message=f"Re-merge failed: {exc}",
            )
        return
    finally:
        if on_complete:
            on_complete()

    if progress:
        progress.update(
            job_id_guard=job_id,
            status="complete",
            state_label="Completed",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
