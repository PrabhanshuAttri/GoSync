import argparse
import os
from pathlib import Path


DEFAULT_DATA_DIR = os.getenv("DATA_DIR", "/data")
DEFAULT_OUTPUT_FOLDER = os.getenv(
    "DOWNLOAD_FOLDER",
    os.getenv("OUTPUT_FOLDER", "downloads"),
)
DEFAULT_COMPLETED_LOG = os.getenv("COMPLETED_LOG", "completed_ids.txt")
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
DEFAULT_MAX_RETRY_PASSES = int(os.getenv("MAX_RETRY_PASSES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
ACCESS_LOGS = os.getenv("ACCESS_LOGS", "false").lower() in {"1", "true", "yes", "on"}


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
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the downloader once from the command line instead of starting the web UI.",
    )
    return parser.parse_args()


def resolve_inside_data_dir(data_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return data_dir / path
