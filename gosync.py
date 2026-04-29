import argparse
import json
import os
import re
import time
import zipfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ChunkedEncodingError, ConnectionError, ReadTimeout
from urllib3.util.retry import Retry

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


DEFAULT_DATA_DIR = os.getenv("DATA_DIR", "/data")
DEFAULT_OUTPUT_FOLDER = os.getenv(
    "DOWNLOAD_FOLDER",
    os.getenv("OUTPUT_FOLDER", "downloads"),
)
DEFAULT_COMPLETED_LOG = os.getenv("COMPLETED_LOG", "completed_ids.txt")
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
DEFAULT_MAX_RETRY_PASSES = int(os.getenv("MAX_RETRY_PASSES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
ZIP_URL_PREFIX = "https://api.gopro.com/media/x/zip/source"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/zip,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://gopro.com",
    "Referer": "https://gopro.com/",
}
SKIPPED_HAR_HEADERS = {
    ":authority",
    ":method",
    ":path",
    ":scheme",
    "accept-encoding",
    "connection",
    "content-length",
    "host",
}
RETRYABLE_DOWNLOAD_ERRORS = (
    ChunkedEncodingError,
    ConnectionError,
    ReadTimeout,
)

ID_PATTERNS = (
    re.compile(r'\\"id\\":\\"([a-zA-Z0-9]{13})\\"'),
    re.compile(r'"id"\s*:\s*"([a-zA-Z0-9]{13})"'),
)


def resolve_har_file(data_dir: Path, har_file: str | None) -> Path:
    if har_file:
        candidate = Path(har_file)
        if not candidate.is_absolute():
            candidate = data_dir / candidate
        if candidate.exists():
            return candidate
        raise FileNotFoundError(f"Could not find HAR file: {candidate}")

    preferred = data_dir / "gopro.com.har"
    if preferred.exists():
        return preferred

    har_files = sorted(data_dir.glob("*.har"))
    if len(har_files) == 1:
        return har_files[0]
    if not har_files:
        raise FileNotFoundError(f"No .har file found in {data_dir}")

    names = ", ".join(path.name for path in har_files)
    raise FileNotFoundError(
        f"Found multiple HAR files in {data_dir}: {names}. Set HAR_FILE or pass --har-file."
    )


def extract_ids(har_path: Path) -> list[str]:
    print(f"\n--- STEP 1: Scanning {har_path} ---", flush=True)
    content = har_path.read_text(encoding="utf-8", errors="ignore")

    found_ids: set[str] = set()
    for pattern in ID_PATTERNS:
        found_ids.update(pattern.findall(content))

    if not found_ids:
        raise ValueError(
            "No media IDs found. Refresh the GoPro media page, scroll to the bottom, "
            "then export the HAR file again."
        )

    ids = sorted(found_ids)
    print(f"Success: found {len(ids)} unique media IDs.", flush=True)
    return ids


def extract_browser_headers(har_path: Path) -> dict[str, str]:
    headers = dict(DEFAULT_HEADERS)

    try:
        with har_path.open("r", encoding="utf-8", errors="ignore") as file:
            har = json.load(file)
    except json.JSONDecodeError:
        print(
            "Warning: could not parse HAR as JSON. Continuing with default headers only.",
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
        headers["User-Agent"] = DEFAULT_USER_AGENT

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


def get_completed_ids(completed_log: Path) -> set[str]:
    completed_log.parent.mkdir(parents=True, exist_ok=True)
    completed_log.touch(exist_ok=True)

    if not completed_log.exists():
        return set()

    content = completed_log.read_text(encoding="utf-8", errors="ignore")
    return {media_id.strip() for media_id in content.split(",") if media_id.strip()}


def log_completed_ids(completed_log: Path, batch_ids: list[str]) -> None:
    completed_log.parent.mkdir(parents=True, exist_ok=True)
    with completed_log.open("a", encoding="utf-8") as file:
        file.write(",".join(batch_ids) + ",")


def build_batches(ids: list[str], batch_size: int) -> list[list[str]]:
    return [ids[index : index + batch_size] for index in range(0, len(ids), batch_size)]


def safe_extract(zip_ref: zipfile.ZipFile, output_dir: Path) -> None:
    output_root = output_dir.resolve()

    for member in zip_ref.infolist():
        target_path = (output_root / member.filename).resolve()
        if output_root not in target_path.parents and target_path != output_root:
            raise ValueError(f"Unsafe path in zip archive: {member.filename}")

    zip_ref.extractall(output_root)


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

            with temp_zip.open("wb") as file, tqdm(
                desc="Downloading",
                total=total_size,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
            ) as progress_bar:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        file.write(chunk)
                        progress_bar.update(len(chunk))
    except RETRYABLE_DOWNLOAD_ERRORS as exc:
        raise RuntimeError(f"Retryable download error: {exc}") from exc


def process_pipeline(
    all_ids: list[str],
    data_dir: Path,
    output_dir: Path,
    completed_log: Path,
    headers: dict[str, str],
    batch_size: int,
    max_retry_passes: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_zip = data_dir / "gopro_temp_batch.zip"
    session = create_session()

    completed_ids = get_completed_ids(completed_log)
    pending_ids = [media_id for media_id in all_ids if media_id not in completed_ids]

    if not pending_ids:
        print(f"\nAll {len(all_ids)} media files have already been downloaded.", flush=True)
        return

    print(f"\n--- STEP 2: Processing {len(pending_ids)} remaining files ---", flush=True)
    pending_batches = build_batches(pending_ids, batch_size)
    pass_number = 1

    while pending_batches:
        if max_retry_passes and pass_number > max_retry_passes:
            raise RuntimeError(
                f"Stopped after {max_retry_passes} retry passes with "
                f"{len(pending_batches)} batch(es) still failing."
            )

        if pass_number > 1:
            print(
                f"\n--- RETRY PASS {pass_number}: {len(pending_batches)} failed batch(es) ---",
                flush=True,
            )

        failed_batches: list[list[str]] = []

        for index, batch in enumerate(pending_batches, start=1):
            print(
                f"\nProcessing batch {index} of {len(pending_batches)} "
                f"({len(batch)} file(s))...",
                flush=True,
            )

            try:
                download_batch(session, batch, temp_zip, headers)

                try:
                    with zipfile.ZipFile(temp_zip) as zip_ref:
                        bad_file = zip_ref.testzip()
                        if bad_file:
                            raise RuntimeError(f"Corrupt file inside zip archive: {bad_file}")
                        print(f"Extracting to {output_dir}...", flush=True)
                        safe_extract(zip_ref, output_dir)
                except zipfile.BadZipFile as exc:
                    raise RuntimeError("Downloaded file is not a valid zip archive.") from exc

                temp_zip.unlink(missing_ok=True)
                log_completed_ids(completed_log, batch)
                print("Batch extracted and logged.", flush=True)

            except Exception as exc:
                print(f"Batch failed; queued for retry: {exc}", flush=True)
                temp_zip.unlink(missing_ok=True)
                failed_batches.append(batch)
                time.sleep(2)

        pending_batches = failed_batches
        pass_number += 1

        if pending_batches:
            print("\nWaiting 5 seconds before retrying failed batches...", flush=True)
            time.sleep(5)

    print(f"\nDone. Recovered media is in {output_dir}.", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and recover GoPro cloud media from a HAR file."
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Mounted directory containing the HAR file and receiving output.",
    )
    parser.add_argument(
        "--har-file",
        default=os.getenv("HAR_FILE"),
        help="HAR filename or path. Defaults to gopro.com.har, then any single *.har file.",
    )
    parser.add_argument(
        "--output-folder",
        default=DEFAULT_OUTPUT_FOLDER,
        help="Download folder name or path. Relative paths are created inside data-dir.",
    )
    parser.add_argument(
        "--completed-log",
        default=DEFAULT_COMPLETED_LOG,
        help="Completion ledger name or path. Relative paths are created inside data-dir.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Number of media IDs to request in each zip batch.",
    )
    parser.add_argument(
        "--max-retry-passes",
        type=int,
        default=DEFAULT_MAX_RETRY_PASSES,
        help="Maximum retry passes for failed batches. Use 0 to retry forever.",
    )
    return parser.parse_args()


def resolve_inside_data_dir(data_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return data_dir / path


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")

    har_path = resolve_har_file(data_dir, args.har_file)
    output_dir = resolve_inside_data_dir(data_dir, args.output_folder).resolve()
    completed_log = resolve_inside_data_dir(data_dir, args.completed_log).resolve()

    print("========================================", flush=True)
    print("             GoSync Utility             ", flush=True)
    print("========================================", flush=True)
    print(f"Data directory: {data_dir}", flush=True)
    print(f"HAR file: {har_path}", flush=True)
    print(f"Output folder: {output_dir}", flush=True)
    print(f"Completed log: {completed_log}", flush=True)
    print(f"Batch size: {args.batch_size}", flush=True)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
