import argparse
import os
from pathlib import Path

from gosync import __version__
from gosync.constants import (
    DEFAULT_DATA_DIR as FALLBACK_DATA_DIR,
)
from gosync.constants import (
    DEFAULT_DOWNLOAD_FOLDER,
    DEFAULT_STATE_FILE,
)
from gosync.constants import (
    DEFAULT_SIDECAR_FOLDER as FALLBACK_SIDECAR_FOLDER,
)

ENV = os.getenv("ENV", "production").lower()
IS_PROD = ENV in {"prod", "production"}
DEBUG = ENV in {"dev", "development"}
DEFAULT_DATA_DIR = os.getenv("DATA_DIR", FALLBACK_DATA_DIR)
DEFAULT_OUTPUT_FOLDER = os.getenv(
    "DOWNLOAD_FOLDER",
    os.getenv("OUTPUT_FOLDER", DEFAULT_DOWNLOAD_FOLDER),
)
DEFAULT_SIDECAR_FOLDER = os.getenv("SIDECAR_FOLDER", FALLBACK_SIDECAR_FOLDER)
DEFAULT_STATE_PATH = os.getenv("GOSYNC_STATE_FILE", DEFAULT_STATE_FILE)
DEFAULT_BATCH_MAX_BYTES = os.getenv("BATCH_MAX_BYTES", "auto")
DEFAULT_BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
DEFAULT_MAX_RETRY_PASSES = int(os.getenv("MAX_RETRY_PASSES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
DISPLAY_WEB_PORT = int(os.getenv("GOSYNC_WEB_PORT", str(WEB_PORT)))
ACCESS_LOGS = os.getenv("ACCESS_LOGS", "true").lower() in {"1", "true", "yes", "on"}




def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download GoPro Cloud media from a HAR file."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"GoSync {__version__}",
    )
    parser.add_argument(
        "--data-dir",
        default=DEFAULT_DATA_DIR,
        help="Mounted directory containing the HAR file and receiving output.",
    )
    parser.add_argument(
        "--har-file",
        default=os.getenv("HAR_FILE"),
        help=(
            "HAR filename or path. Defaults to gopro.com.har, then any single "
            "*.har file."
        ),
    )
    parser.add_argument(
        "--output-folder",
        default=DEFAULT_OUTPUT_FOLDER,
        help=(
            "Download folder name or path. Relative paths are created inside "
            "data-dir."
        ),
    )
    parser.add_argument(
        "--sidecar-folder",
        default=DEFAULT_SIDECAR_FOLDER,
        help=(
            "Deprecated. XMP sidecars are written next to media files inside "
            "the download folder."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_PATH,
        help="JSON state filename or path. Relative paths are created inside data-dir.",
    )
    parser.add_argument(
        "--batch-max-bytes",
        default=DEFAULT_BATCH_MAX_BYTES,
        help=(
            "Maximum total source bytes per download batch. Use 'auto' to use "
            "the largest file_size from the HAR manifest."
        ),
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
        help=(
            "Run the downloader once from the command line instead of starting "
            "the web UI."
        ),
    )
    return parser.parse_args()


def resolve_inside_data_dir(data_dir: Path, value: str) -> Path:
    data_dir = data_dir.resolve()
    path = Path(value)
    candidate = path if path.is_absolute() else data_dir / path
    candidate = candidate.resolve()
    try:
        candidate.relative_to(data_dir)
    except ValueError as exc:
        raise ValueError(f"Path must be inside data directory: {data_dir}") from exc
    return candidate
