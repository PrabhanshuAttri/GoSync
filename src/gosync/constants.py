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
DEFAULT_EXIFTOOL_BINARY = "exiftool"
DEFAULT_EXIFTOOL_TIMEOUT_SECONDS = 300
DEFAULT_FFPROBE_BINARY = "ffprobe"
DEFAULT_FFPROBE_TIMEOUT_SECONDS = 30

# Chapter-merge size tolerance. Merged files are rewritten by ffmpeg/exiftool
# and rarely land at exactly the API's per-chapter file_size sum (container
# remux overhead differs). Measured across a real library (see
# scripts/measure_chapter_merge_size_delta.py), that overhead is roughly
# *constant per chapter* (ffmpeg's concat consolidates each chapter's own
# container overhead into one copy instead of N), not proportional to total
# file size -- so tolerance is chapter-count-scaled absolute bytes, with an
# absolute floor (for small clips) and ceiling (so a multi-GB file can't
# inherit an unbounded blind spot the way a percentage-of-size band would).
# ~1.5 MiB/chapter measured, with a 5-10x safety margin baked in.
DEFAULT_PER_CHAPTER_OVERHEAD_BYTES = 10 * 1024 * 1024
DEFAULT_MIN_TOLERANCE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TOLERANCE_BYTES = 200 * 1024 * 1024

# Caps how many chaptered items the local-merge short-circuit will process
# (ffmpeg + exiftool, potentially large files) in a single process_pipeline
# run, so one run can't serially chew through an unbounded backlog and hold
# whatever lock it runs under for an unpredictable amount of time. The rest
# are simply left pending and picked up on the next run.
DEFAULT_MAX_LOCAL_MERGES_PER_RUN = 10

MEDIA_SEARCH_URL = "https://api.gopro.com/media/search"
ZIP_URL_PREFIX = "https://api.gopro.com/media/x/zip/source"
MEDIA_DOWNLOAD_URL = "https://api.gopro.com/media"
MEDIA_LIST_KEYS = ("media", "items", "data")

API_MEDIA_SEARCH_FIELDS = (
    "id,filename,file_size,camera_model,captured_at,captured_at_timezone,"
    "created_at,type,content_type,content_title,content_description,tags,"
    "file_extension,item_count,width,height"
)
API_MEDIA_SEARCH_PER_PAGE = 100

# Header values confirmed working against GoPro's JSON media API by the
# reverse-engineered dustin/gopro-plus client (see docs/gopro-api-reference.md).
# Distinct from DEFAULT_HEADERS, which targets the zip-export endpoint.
API_JSON_ACCEPT = "application/vnd.gopro.jk.media+json; version=2.0.0"
API_JSON_USER_AGENT = "github.com/dustin/gopro-plus 0.6.0.3"

AUTH_METHOD_HAR = "har"
AUTH_METHOD_API_TOKEN = "api_token"

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
