DEFAULT_DATA_DIR = "/data"
DEFAULT_DOWNLOAD_FOLDER = "downloads"
ORIGINAL_UNMERGED_FOLDER_PREFIX = "original_unmerged"
DEFAULT_HAR_FILE = "gopro.com.har"
DEFAULT_STATE_FILE = "gosync_state.json"
DEFAULT_MANIFEST_FILE = "manifest.json"
DEFAULT_MEDIA_RESPONSES_FILE = "media_search.json"
DEFAULT_TEMP_ZIP = "gopro_temp_batch.zip"
REPORTS_FOLDER = "reports"
DEFAULT_FFMPEG_BINARY = "ffmpeg"
DEFAULT_FFMPEG_TIMEOUT_SECONDS = 900

MEDIA_SEARCH_URL = "https://api.gopro.com/media/search"
ZIP_URL_PREFIX = "https://api.gopro.com/media/x/zip/source"
MEDIA_LIST_KEYS = ("media", "items", "data")

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

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DOWNLOADED = "downloaded"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

MAX_SINGLE_FILE_RETRIES = 3

COMMON_SIDECAR_FIELDS = {
    "ai_training_opt_out",
    "camera_model",
    "captured_at",
    "captured_at_timezone",
    "content_title",
    "content_type",
    "created_at",
    "file_extension",
    "file_size",
    "filename",
    "firmware_version",
    "fov",
    "height",
    "orientation",
    "play_as",
    "ready_to_view",
    "resolution",
    "submitted_at",
    "thumbnail_available",
    "type",
    "updated_at",
    "width",
}
VIDEO_SIDECAR_FIELDS = COMMON_SIDECAR_FIELDS | {
    "available_labels",
    "mce_type",
    "moments_count",
    "ready_to_edit",
    "source_duration",
    "stabilized",
}
IMAGE_SIDECAR_FIELDS = COMMON_SIDECAR_FIELDS
