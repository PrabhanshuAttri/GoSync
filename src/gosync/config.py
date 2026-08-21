import argparse
import os
from pathlib import Path

from gosync import __version__
from gosync.constants import (
    AUTH_METHOD_HAR,
    DEFAULT_DOWNLOAD_FOLDER,
    DEFAULT_EXIFTOOL_BINARY,
    DEFAULT_EXIFTOOL_TIMEOUT_SECONDS,
    DEFAULT_FFMPEG_BINARY,
    DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    DEFAULT_FFPROBE_BINARY,
    DEFAULT_FFPROBE_TIMEOUT_SECONDS,
    DEFAULT_MAX_LOCAL_MERGES_PER_RUN,
    DEFAULT_MAX_TOLERANCE_BYTES,
    DEFAULT_MIN_TOLERANCE_BYTES,
    DEFAULT_PER_CHAPTER_OVERHEAD_BYTES,
    DEFAULT_STATE_FILE,
)
from gosync.constants import (
    DEFAULT_DATA_DIR as FALLBACK_DATA_DIR,
)

ENV = os.getenv("ENV", "production").lower()
IS_PROD = ENV in {"prod", "production"}
DEBUG = ENV in {"dev", "development"}
DEFAULT_DATA_DIR = os.getenv("DATA_DIR", FALLBACK_DATA_DIR)
DEFAULT_OUTPUT_FOLDER = os.getenv(
    "DOWNLOAD_FOLDER",
    os.getenv("OUTPUT_FOLDER", DEFAULT_DOWNLOAD_FOLDER),
)
DEFAULT_STATE_PATH = os.getenv("GOSYNC_STATE_FILE", DEFAULT_STATE_FILE)
DEFAULT_BATCH_MAX_BYTES = os.getenv("BATCH_MAX_BYTES", "auto")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "60"))
DEFAULT_AUTH_METHOD = os.getenv("AUTH_METHOD", AUTH_METHOD_HAR).strip().lower()
DEFAULT_AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
DEFAULT_USER_ID = os.getenv("USER_ID", "")
DOWNLOAD_TELEMETRY = os.getenv("DOWNLOAD_TELEMETRY", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CREATE_XMP_SIDECARS = os.getenv("CREATE_XMP_SIDECARS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", DEFAULT_FFMPEG_BINARY)
FFMPEG_TIMEOUT_SECONDS = int(
    os.getenv("FFMPEG_TIMEOUT_SECONDS", str(DEFAULT_FFMPEG_TIMEOUT_SECONDS))
)
EXIFTOOL_BINARY = os.getenv("EXIFTOOL_BINARY", DEFAULT_EXIFTOOL_BINARY)
EXIFTOOL_TIMEOUT_SECONDS = int(
    os.getenv("EXIFTOOL_TIMEOUT_SECONDS", str(DEFAULT_EXIFTOOL_TIMEOUT_SECONDS))
)
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", DEFAULT_FFPROBE_BINARY)
FFPROBE_TIMEOUT_SECONDS = int(
    os.getenv("FFPROBE_TIMEOUT_SECONDS", str(DEFAULT_FFPROBE_TIMEOUT_SECONDS))
)
PER_CHAPTER_OVERHEAD_BYTES = int(
    os.getenv("PER_CHAPTER_OVERHEAD_BYTES", str(DEFAULT_PER_CHAPTER_OVERHEAD_BYTES))
)
MIN_TOLERANCE_BYTES = int(
    os.getenv("MIN_TOLERANCE_BYTES", str(DEFAULT_MIN_TOLERANCE_BYTES))
)
MAX_TOLERANCE_BYTES = int(
    os.getenv("MAX_TOLERANCE_BYTES", str(DEFAULT_MAX_TOLERANCE_BYTES))
)
MAX_LOCAL_MERGES_PER_RUN = int(
    os.getenv("MAX_LOCAL_MERGES_PER_RUN", str(DEFAULT_MAX_LOCAL_MERGES_PER_RUN))
)
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
            "HAR filename inside data-dir. Defaults to gopro.com.har, then any "
            "single *.har file."
        ),
    )
    parser.add_argument(
        "--auth-method",
        default=DEFAULT_AUTH_METHOD,
        choices=["har", "api_token"],
        help=(
            "How to authenticate against GoPro Cloud: 'har' (default, export a "
            "browser HAR) or 'api_token' (paste a captured bearer token)."
        ),
    )
    parser.add_argument(
        "--auth-token",
        default=DEFAULT_AUTH_TOKEN or None,
        help="GoPro bearer token. Required when --auth-method=api_token.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID or None,
        help=(
            "Optional GoPro account/user id used to scope --auth-method=api_token "
            "media searches."
        ),
    )
    parser.add_argument(
        "--download-telemetry",
        action="store_true",
        default=DOWNLOAD_TELEMETRY,
        help=(
            "Also fetch per-item mediainfo.json and GPX/GPMF telemetry "
            "sidecars. Off by default -- adds extra live API calls per item."
        ),
    )
    parser.add_argument(
        "--no-create-xmp-sidecars",
        dest="create_xmp_sidecars",
        action="store_false",
        default=CREATE_XMP_SIDECARS,
        help=(
            "Skip generating Immich-compatible XMP sidecar files next to "
            "each downloaded media file. On by default."
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
