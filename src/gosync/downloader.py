import json
import time
import zipfile
from collections import deque
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ChunkedEncodingError, ConnectionError, ReadTimeout
from urllib3.util.retry import Retry

from gosync.config import REQUEST_TIMEOUT
from gosync.constants import (
    DEFAULT_HAR_FILE,
    DEFAULT_HEADERS,
    DEFAULT_TEMP_ZIP,
    MAX_SINGLE_FILE_RETRIES,
    SKIPPED_HAR_HEADERS,
    STATUS_DOWNLOADED,
    ZIP_URL_PREFIX,
)
from gosync.manifest import MediaItem
from gosync.paths import media_download_path, safe_child_path
from gosync.progress import ProgressState
from gosync.state import (
    mark_downloaded,
    mark_failed,
    pending_keys,
)

try:
    from tqdm import tqdm
except ImportError:
    class tqdm:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, _amount: int) -> None:
            return None


RETRYABLE_DOWNLOAD_ERRORS = (
    ChunkedEncodingError,
    ConnectionError,
    ReadTimeout,
)


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else plural or f"{singular}s"


def format_size_mib(size_bytes: int | None) -> str:
    if not size_bytes:
        return "unknown size"
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def format_media_for_log(item: MediaItem) -> str:
    return f"{item.filename} ({item.media_id}, {format_size_mib(item.file_size)})"


class DownloadCancelled(Exception):
    pass


def resolve_har_file(data_dir: Path, har_file: str | None) -> Path:
    data_dir = data_dir.resolve()
    if har_file:
        candidate = Path(har_file)
        if not candidate.is_absolute():
            candidate = data_dir / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(data_dir)
        except ValueError as exc:
            raise FileNotFoundError(
                f"HAR file must be inside data directory: {data_dir}"
            ) from exc
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Could not find HAR file: {candidate}")

    preferred = data_dir / DEFAULT_HAR_FILE
    if preferred.exists():
        return preferred

    har_files = sorted(data_dir.glob("*.har"))
    if len(har_files) == 1:
        return har_files[0]
    if not har_files:
        raise FileNotFoundError(f"No .har file found in {data_dir}")

    names = ", ".join(path.name for path in har_files)
    raise FileNotFoundError(
        f"Found multiple HAR files in {data_dir}: {names}. "
        "Set HAR_FILE or pass --har-file."
    )


def extract_browser_headers(har_path: Path) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)

    try:
        with har_path.open("r", encoding="utf-8", errors="ignore") as file:
            har = json.load(file)
    except json.JSONDecodeError:
        print(
            "Warning: could not parse HAR as JSON. "
            "Continuing with default headers only.",
            flush=True,
        )
        return headers

    entries = har.get("log", {}).get("entries", [])
    best_match = None

    for entry in entries:
        request = entry.get("request", {})
        url = request.get("url", "")
        if "api.gopro.com" in url or "gopro.com" in url:
            best_match = request
            if url.startswith(ZIP_URL_PREFIX):
                break

    if not best_match:
        print(
            "Warning: no GoPro browser request headers found in the HAR. "
            "Continuing with default headers only.",
            flush=True,
        )
        return headers

    copied_headers: set[str] = set()
    for header in best_match.get("headers", []):
        name = header.get("name", "")
        value = header.get("value", "")
        normalized_name = name.lower()

        if not name or not value or normalized_name in SKIPPED_HAR_HEADERS:
            continue

        headers[name] = value
        copied_headers.add(normalized_name)

    if "user-agent" not in copied_headers:
        headers["User-Agent"] = DEFAULT_HEADERS["User-Agent"]

    copied_sensitive = sorted({"authorization", "cookie"}.intersection(copied_headers))
    if copied_sensitive:
        print(
            f"Reusing browser session header(s): {', '.join(copied_sensitive)}",
            flush=True,
        )
    else:
        print(
            "Warning: HAR did not include Cookie or Authorization headers. "
            "If downloads return 403, export a fresh HAR while logged in.",
            flush=True,
        )

    return headers

def parse_batch_max_bytes(value: str | int | None, media_items: list[MediaItem]) -> int:
    valid_sizes = [item.file_size for item in media_items if item.file_size]
    largest_file_size = max(valid_sizes) if valid_sizes else 0
    if value in (None, "", "auto"):
        return largest_file_size
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("--batch-max-bytes must be an integer or 'auto'") from None
    if parsed < 1:
        raise ValueError("--batch-max-bytes must be greater than zero")
    if largest_file_size:
        return min(parsed, largest_file_size)
    return parsed


def build_size_batches(
    media_items: list[MediaItem],
    batch_max_bytes: int,
    batch_file_limit: int | None = None,
    clamp_to_largest_file: bool = True,
) -> list[list[MediaItem]]:
    if batch_file_limit is not None and batch_file_limit < 1:
        raise ValueError("batch_file_limit must be greater than zero")

    unknown_size = [item for item in media_items if not item.file_size]
    known_size = sorted(
        (item for item in media_items if item.file_size),
        key=lambda item: (item.file_size or 0, item.filename),
        reverse=True,
    )
    largest_file_size = known_size[0].file_size if known_size else 0
    if clamp_to_largest_file and largest_file_size:
        batch_max_bytes = min(batch_max_bytes, largest_file_size)

    batches: list[list[MediaItem]] = []
    batch_sizes: list[int] = []
    for item in known_size:
        item_size = item.file_size or 0
        placed = False
        for index, batch_size in enumerate(batch_sizes):
            batch_has_room = (
                batch_file_limit is None or len(batches[index]) < batch_file_limit
            )
            if batch_has_room and batch_size + item_size <= batch_max_bytes:
                batches[index].append(item)
                batch_sizes[index] += item_size
                placed = True
                break
        if not placed:
            batches.append([item])
            batch_sizes.append(item_size)

    for item in unknown_size:
        batches.append([item])

    return batches


def parse_batch_file_limit(value: str | int | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("files per batch must be a positive integer") from None
    if parsed < 1:
        raise ValueError("files per batch must be greater than zero")
    return parsed


def completed_count_for_items(state: dict, media_items: list[MediaItem]) -> int:
    media = state.get("media", {})
    if not isinstance(media, dict):
        return 0
    return sum(
        1
        for item in media_items
        if isinstance(media.get(item.key), dict)
        and media[item.key].get("download_status") == STATUS_DOWNLOADED
    )


def safe_extract(zip_ref: zipfile.ZipFile, output_dir: Path) -> None:
    output_root = output_dir.resolve()

    for member in zip_ref.infolist():
        target_path = (output_root / member.filename).resolve()
        if output_root not in target_path.parents and target_path != output_root:
            raise ValueError(f"Unsafe path in zip archive: {member.filename}")

    zip_ref.extractall(output_root)


def organize_extracted_media(output_dir: Path, media_items: list[MediaItem]) -> None:
    for item in media_items:
        target_path = media_download_path(output_dir, item.filename)
        if not safe_child_path(output_dir, target_path):
            raise ValueError(f"Unsafe media path: {item.filename}")
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            continue

        source_path = output_dir / item.filename
        if not safe_child_path(output_dir, source_path):
            continue
        if source_path.exists():
            source_path.replace(target_path)


def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def download_batch(
    session: requests.Session,
    batch: list[str],
    temp_zip: Path,
    headers: dict[str, str],
    progress: ProgressState | None = None,
    job_id: str | None = None,
) -> None:
    batch_str = ",".join(batch)
    url = f"{ZIP_URL_PREFIX}?ids={batch_str}"

    try:
        with session.get(
            url,
            headers=headers,
            stream=True,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("content-length", 0))
            download_started_at = time.monotonic()
            if progress:
                progress.update(
                    job_id_guard=job_id,
                    state_label="Downloading",
                    current_download_bytes=0,
                    current_download_total=total_size,
                    current_download_started_at=download_started_at,
                    current_download_speed_bps=0,
                    current_download_elapsed_seconds=0,
                )

            with temp_zip.open("wb") as file, tqdm(
                desc="Downloading",
                total=total_size,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
            ) as progress_bar:
                for chunk in response.iter_content(chunk_size=8192):
                    if progress and progress.stop_requested:
                        raise DownloadCancelled("Download stop requested.")
                    if chunk:
                        file.write(chunk)
                        progress_bar.update(len(chunk))
                        if progress:
                            current_bytes = progress.increment(
                                "current_download_bytes",
                                len(chunk),
                                job_id_guard=job_id,
                            )
                            if current_bytes is None:
                                continue
                            elapsed = max(time.monotonic() - download_started_at, 0.001)
                            progress.update(
                                job_id_guard=job_id,
                                current_download_elapsed_seconds=elapsed,
                                current_download_speed_bps=current_bytes / elapsed,
                            )
    except RETRYABLE_DOWNLOAD_ERRORS as exc:
        raise RuntimeError(f"Retryable download error: {exc}") from exc


def process_pipeline(
    media_items: list[MediaItem],
    data_dir: Path,
    output_dir: Path,
    state_file: Path,
    headers: dict[str, str],
    batch_max_bytes: str | int | None,
    progress: ProgressState | None = None,
    batch_file_limit: str | int | None = None,
    batch_cap_media_items: list[MediaItem] | None = None,
    job_id: str | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_zip = data_dir / DEFAULT_TEMP_ZIP
    session = create_session()

    state_keys = pending_keys(mark_downloaded(state_file, []))
    pending_items = [item for item in media_items if item.key in state_keys]
    completed_count = len(media_items) - len(pending_items)
    if progress:
        progress.update(
            job_id_guard=job_id,
            total_ids=len(media_items),
            completed_ids=completed_count,
            pending_ids=len(pending_items),
            output_dir=str(output_dir),
            failed_batches=0,
        )

    if not pending_items:
        message = f"All {len(media_items)} media files have already been downloaded."
        if progress:
            progress.log(message, job_id_guard=job_id)
        else:
            print(f"\n{message}", flush=True)
        return

    batch_cap_items = batch_cap_media_items or media_items
    batch_cap = parse_batch_max_bytes(batch_max_bytes, batch_cap_items)
    file_limit = parse_batch_file_limit(batch_file_limit)
    pending_batches = deque(
        build_size_batches(
            pending_items,
            batch_cap,
            file_limit,
            clamp_to_largest_file=batch_cap_media_items is None,
        )
    )
    total_batches = len(pending_batches)
    if progress:
        progress.update(
            job_id_guard=job_id,
            total_batches=total_batches,
            completed_batches=0,
            failed_batches=0,
        )

    print(
        f"\n--- STEP 2: Processing {len(pending_items)} remaining files ---",
        flush=True,
    )
    if progress:
        progress.log(
            f"Using download batch size cap: {batch_cap or 'unknown'} bytes",
            job_id_guard=job_id,
        )
        if file_limit:
            progress.log(
                f"Using download files per batch cap: {file_limit}",
                job_id_guard=job_id,
            )
    else:
        print(
            f"Using download batch size cap: {batch_cap or 'unknown'} bytes",
            flush=True,
        )
        if file_limit:
            print(f"Using download files per batch cap: {file_limit}", flush=True)

    batch_index = 0
    while pending_batches:
        if progress and progress.stop_requested:
            raise DownloadCancelled("Download stop requested.")

        batch = pending_batches.popleft()
        batch_index += 1
        batch_ids = [item.media_id for item in batch]
        batch_keys = [item.key for item in batch]
        batch_file_list = "\n".join(
            f"  - {format_media_for_log(item)}" for item in batch
        )
        message = (
            f"Processing batch {batch_index} ({len(batch)} "
            f"{pluralize(len(batch), 'file')})\nFiles in batch:\n{batch_file_list}"
        )
        if progress:
            progress.update(
                job_id_guard=job_id,
                state_label="Processing",
                current_batch=batch_index,
                current_batch_size=len(batch),
                current_batch_keys=batch_keys,
                current_download_bytes=0,
                current_download_total=0,
                current_download_started_at=0,
                current_download_speed_bps=0,
                current_download_elapsed_seconds=0,
            )
            progress.log(message, job_id_guard=job_id)
        else:
            print(f"\n{message}", flush=True)

        try:
            download_batch(session, batch_ids, temp_zip, headers, progress, job_id)

            try:
                with zipfile.ZipFile(temp_zip) as zip_ref:
                    bad_file = zip_ref.testzip()
                    if bad_file:
                        raise RuntimeError(
                            f"Corrupt file inside zip archive: {bad_file}"
                        )
                    if progress:
                        progress.update(
                            job_id_guard=job_id,
                            state_label="Extracting",
                            current_download_bytes=0,
                            current_download_total=0,
                            current_download_started_at=0,
                            current_download_speed_bps=0,
                            current_download_elapsed_seconds=0,
                        )
                    print(f"Extracting to {output_dir}...", flush=True)
                    safe_extract(zip_ref, output_dir)
                    organize_extracted_media(output_dir, batch)
            except zipfile.BadZipFile as exc:
                raise RuntimeError(
                    "Downloaded file is not a valid zip archive."
                ) from exc

            temp_zip.unlink(missing_ok=True)
            state = mark_downloaded(state_file, batch_keys)
            completed_count = completed_count_for_items(state, media_items)
            downloaded_list = "\n".join(
                f"  - {format_media_for_log(item)}" for item in batch
            )
            if progress:
                progress.update(
                    job_id_guard=job_id,
                    completed_ids=completed_count,
                    pending_ids=max(len(media_items) - completed_count, 0),
                    current_download_bytes=0,
                    current_download_total=0,
                    current_download_started_at=0,
                    current_download_speed_bps=0,
                    current_download_elapsed_seconds=0,
                    current_batch_keys=[],
                    failed_batches=0,
                )
                progress.increment("completed_batches", 1, job_id_guard=job_id)
                progress.log_background(
                    f"Batch downloaded:\n{downloaded_list}",
                    job_id_guard=job_id,
                )
                progress.notify(
                    "success",
                    "Batch complete",
                    f"{len(batch)} {pluralize(len(batch), 'file')} downloaded.",
                    job_id_guard=job_id,
                )
            else:
                print(f"Batch downloaded:\n{downloaded_list}", flush=True)

        except Exception as exc:
            if isinstance(exc, DownloadCancelled):
                temp_zip.unlink(missing_ok=True)
                raise

            temp_zip.unlink(missing_ok=True)
            state = mark_failed(state_file, batch_keys, str(exc))
            if len(batch) > 1:
                pending_batches.extend([item] for item in batch)
                retry_message = "Split failed batch into single-file retries."
            else:
                record = next(
                    (
                        item
                        for item in state.get("media", {}).values()
                        if isinstance(item, dict) and item.get("key") == batch[0].key
                    ),
                    {},
                )
                retry_count = int(record.get("retry_count") or 0)
                if retry_count < MAX_SINGLE_FILE_RETRIES:
                    pending_batches.append(batch)
                    retry_message = (
                        f"Queued single-file retry {retry_count} of "
                        f"{MAX_SINGLE_FILE_RETRIES}."
                    )
                else:
                    mark_failed(state_file, batch_keys, str(exc), retry=False)
                    retry_message = "Single-file retry limit reached."

            if progress:
                progress.update(job_id_guard=job_id, failed_batches=1)
                progress.update(job_id_guard=job_id, current_batch_keys=[])
                progress.log(
                    f"Batch failed: {exc}\nFiles in failed batch:\n"
                    f"{batch_file_list}\n{retry_message}",
                    job_id_guard=job_id,
                )
                progress.notify(
                    "error",
                    "Batch failed",
                    f"{len(batch)} {pluralize(len(batch), 'file')} failed. "
                    f"{retry_message}",
                    job_id_guard=job_id,
                )
            else:
                print(
                    f"Batch failed: {exc}\nFiles in failed batch:\n"
                    f"{batch_file_list}\n{retry_message}",
                    flush=True,
                )
            time.sleep(2)

    print(f"\nDone. Downloaded media is in {output_dir}.", flush=True)
