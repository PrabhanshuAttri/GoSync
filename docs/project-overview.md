# Project Overview

GoSync is a self-hosted Python and Docker utility for downloading GoPro Cloud
media libraries. It provides a web UI for selecting pending media, running
resumable batch downloads, monitoring progress, and writing downloaded media
plus XMP sidecars into a mounted data directory. It supports two independent
ways to authenticate against GoPro Cloud -- a browser HAR export, or a
directly-pasted API bearer token -- covered in detail below.

## Key Capabilities

- Authenticates via either a browser HAR export or a directly-pasted GoPro
  API bearer token.
- Parses or live-fetches GoPro `media/search` data into a local manifest.
- Downloads pending media in resumable size-based batches.
- Organizes media into extension folders, merging GoPro chapter files into a
  single file via ffmpeg and copying camera/lens metadata onto the merge with
  exiftool.
- Optionally fetches per-item GPX/GPMF telemetry and `mediainfo.json`.
- Generates XMP sidecar files next to downloaded media.
- Supports optional files-per-batch limits from the web UI.
- Tracks durable download state in `gosync_state.json`.
- Provides grouped status events, transfer progress, notifications, and run
  reports.

## Authentication Methods

Every download run needs a `headers` dict usable against `api.gopro.com` and
a `MediaManifest` (the list of media items to download). GoSync builds both
one of two ways, selected by `AUTH_METHOD`:

### HAR method (`AUTH_METHOD=har`, the default)

You export a full browser HAR while logged into gopro.com and upload it.
GoSync:

- Parses the HAR's cached `media/search` response bodies into the manifest --
  this only covers whatever the browser happened to load while you were
  browsing your library, not necessarily every item in the account.
- Copies `Cookie`/`Authorization` headers off a captured request in that same
  HAR to use as `headers` for the zip-export download.

### API token method (`AUTH_METHOD=api_token`)

You capture one bearer token (and optionally your GoPro user id) from a
DevTools request instead of exporting a whole HAR file. GoSync:

- Builds `headers` directly from the token (`Authorization: Bearer <token>`)
  -- no file to parse, nothing to capture live for auth.
- Paginates the live `media/search` endpoint using those headers, covering
  the *entire* library rather than only what a browser session happened to
  load. `user_id` is optional -- the bearer token alone is sufficient to
  authenticate; user id only scopes/filters the search.

Neither method is an official OAuth flow -- both are the same
reverse-engineered GoPro Plus API, and the captured token/session expires
like any browser session (there's no refresh token). A 403 mid-run means
re-export the HAR, or paste a fresh token.

### Shared pipeline after auth

Everything downstream of `headers` + manifest is identical regardless of
which method produced them: batching, the zip-export download, extraction,
GoPro chapter merging, state bookkeeping, XMP sidecar generation, and the
optional telemetry fetch (`DOWNLOAD_TELEMETRY=true`, see [media
items](media-items.md)) -- the telemetry step also only needs `headers`, so
it works the same under either auth method.

### Selecting a method

- CLI / env: set `AUTH_METHOD=har` (default, needs `HAR_FILE`) or
  `AUTH_METHOD=api_token` (needs `AUTH_TOKEN`, optionally `USER_ID`), or pass
  `--auth-method`/`--auth-token`/`--user-id`.
- Web UI: the **Settings** panel has a HAR upload/select control and a
  masked **GoPro API token** + optional **User ID** field. Saving settings
  switches the active method automatically: if both a token *and* a user id
  are present, GoSync uses the API token method; otherwise it falls back to
  HAR mode. A previously-saved token is kept if you resave settings with the
  masked field left empty, so other settings changes don't force
  re-pasting it.
- The dashboard's HAR/auth-source label reflects whichever method is active
  ("HAR: gopro.com.har" or "API token").

Auth settings set through the web UI live only in the running container's
memory (not written back to `.env` or a file) -- they reset to whatever
`AUTH_METHOD`/`AUTH_TOKEN`/`USER_ID`/`HAR_FILE` the container started with if
it restarts.

## Runtime Components

- `app.py` / `__main__.py`: application entry point for the web server or
  one-shot CLI run.
- `config.py`: environment variables, defaults, and argument parsing.
- `constants.py`: shared filenames, status values, and API defaults.
- `auth.py`: resolves which authentication method is active and builds the
  `headers` dict for either one.
- `manifest.py`: HAR parsing and live `media/search` pagination, media
  extraction, deduplication, and manifest writing.
- `downloader.py`: HAR header extraction, batching, download streaming, zip
  validation, extraction, chapter merging, retry behavior, and download
  events.
- `telemetry.py`: optional per-item GPX/GPMF telemetry and `mediainfo.json`
  fetching.
- `state.py`: JSON state creation, resume sync, download status, retry count,
  and sidecar/telemetry status updates.
- `progress.py`: in-memory web job state, event normalization, and
  notifications.
- `events.py`: structured event creation, redaction, UI presentation metadata,
  and CLI log formatting.
- `runtime.py`: path preparation, job orchestration, run reports, and CLI
  run-once flow.
- `sidecar.py`: XMP sidecar generation from parsed media metadata.
- `report.py`: run summary/report writing under `reports/`.
- `web.py`: Flask routes for the dashboard, settings, uploads, job control,
  status, current-run events, and media rows.

## Data Flow

1. Auth is resolved: a HAR file is uploaded/selected, or an API token (and
   optional user id) is saved in Settings.
2. GoSync builds `headers` and the manifest (parsed from the HAR, or fetched
   live from the API) and writes `manifest.json`, `media_search.json`, and
   `gosync_state.json`.
3. The web UI starts a job with selected pending media.
4. If `DOWNLOAD_TELEMETRY` is on, per-item GPX/GPMF telemetry and
   `mediainfo.json` are fetched before the media download.
5. Pending media are grouped into batches and downloaded as temporary zip
   files.
6. Each zip is validated, extracted, organized by extension, and then marked
   downloaded in JSON state. If the zip contains extra GoPro chapter files for
   a manifest item, they are merged via ffmpeg into the manifest item's
   single target file (reporting a "Merging" status while ffmpeg runs),
   camera/lens metadata is copied onto the merge with exiftool, and the
   original chapter files are moved into `downloads/original_unmerged_<ext>/`
   -- falling back to separate, un-merged files if ffmpeg is unavailable or
   the merge fails. See [media items](media-items.md) for the full merge
   strategy, including the size-verification tolerance this requires.
7. If `CREATE_XMP_SIDECARS` is on (the default), XMP sidecars are generated
   next to media files.
8. Completion, stop, or failure reports are written under `reports/`.

## Web UI Workflow

The web UI is the default container experience. It is a local dashboard for
managing one download job at a time.

1. Open `http://localhost:49152` when using Docker Compose, or the host port
   you mapped with Docker.
2. In **Settings**, choose an auth method: upload/select a HAR file, or enter
   a GoPro API token (and optional user id) -- see [Authentication
   Methods](#authentication-methods) above.
3. In **Download Job**, choose **Files per batch** when you want the web UI
   to limit how many selected files are placed in each batch.
4. In **Media Files**, select the pending files you want to download.
   Downloaded rows are disabled. You can filter by extension and sort by
   size or status.
5. Click **Start Download**. The browser keeps selected files, sort order,
   and files-per-batch through the start redirect.
6. Click **Stop Download** if you need to cancel the active job. GoSync stops
   safely, removes the temporary zip, and keeps JSON state for resume.
7. Watch **Media Complete**, **Downloaded**, **Batches**, and transfer
   progress update live.
8. Use **Status** for the latest detailed action, such as the batch currently
   being processed.
9. Use **Event Log** for a running history of uploads, extension summaries,
   retries, stops, failures, completed batches, and final run summaries.
10. Use the **Light** or **Dark** toggle in the header to switch themes. The
    browser remembers your choice.

The dashboard stores uploaded HAR files in the mounted data directory.
Downloaded media, generated sidecars, media metadata dumps, JSON state, run
reports, and the temporary batch zip file also live in that same directory.

## Resume Behavior

If the container stops or your network drops, start GoSync again with the
same data directory (and the same auth settings, for API token mode -- see
the note in [Authentication Methods](#authentication-methods) above). The web
UI syncs `gosync_state.json` against the actual files in `downloads/`, marks
missing or size-mismatched files pending again, and skips files that are
already present and correctly sized.

GoSync downloads media in size-based batches. With the default
`BATCH_MAX_BYTES=auto`, the batch cap is the largest `file_size` found in the
manifest. Explicit `BATCH_MAX_BYTES` values above the largest known file are
capped to that largest file size, so a batch should not be larger than the
largest individual media file. When the web UI downloads only selected files,
the automatic byte cap still comes from the full manifest, while the
files-per-batch control limits how many selected files can be placed in one
batch.

## Data Directory Structure

The mounted data directory is the only persistent storage GoSync needs. By
default it looks like this:

```text
data/
|-- gopro.com.har
|-- manifest.json
|-- media_search.json
|-- gosync_state.json
|-- logs/
|   `-- gosync.log
|-- reports/
|   `-- gosync-report-20260501-120000.json
`-- downloads/
    |-- jpg/
    |   |-- example-photo.JPG
    |   `-- example-photo.JPG.xmp
    |-- mp4/
    |   |-- example-video.MP4
    |   `-- example-video.MP4.xmp
    |-- original_unmerged_mp4/
    |   |-- example-chapter-1.MP4
    |   `-- example-chapter-2.MP4
    |-- gpx/
    |   `-- example-video.gpx
    |-- gpmf/
    |   `-- example-video_1.gpmf
    `-- json/
        `-- example-video_mediainfo.json
```

`gopro.com.har` only exists in HAR mode; it's absent when using an API token.

Files and folders:

- `gopro.com.har`: exported browser HAR file, or another filename set with
  `HAR_FILE`. HAR mode only.
- `downloads/`: downloaded media output folder, configurable with
  `DOWNLOAD_FOLDER`. Media files and their XMP sidecars are grouped together in
  extension folders such as `downloads/mp4/` and `downloads/jpg/`.
- `downloads/original_unmerged_<ext>/`: raw per-chapter source files kept
  after a successful chapter merge (see [media items](media-items.md)).
  Only present once a chaptered recording has been downloaded and merged.
- `downloads/gpx/`, `downloads/gpmf/`, `downloads/json/`: optional per-item
  telemetry (`DOWNLOAD_TELEMETRY=true`) -- merged GPX track, raw GPMF
  fallback per chapter, and the combined `/media/{id}` + `mediainfo` record.
  Absent when telemetry fetching is off (the default).
- `manifest.json`: media manifest, parsed from the HAR or fetched live from
  the API depending on the active auth method.
- `media_search.json`: dump of all media objects behind the manifest,
  including duplicates before manifest deduplication.
- `gosync_state.json`: JSON resume state for every parsed media item,
  configurable with `GOSYNC_STATE_FILE`.
- `logs/gosync.log`: rotating file log of the same events shown in the web
  UI's Event Log.
- `reports/`: run reports written when downloads complete or stop.
- `gopro_temp_batch.zip`: temporary zip file used during a batch download;
  deleted after extraction or failure.

See [media items](media-items.md) for HAR-derived media records, extraction
behavior, chapter merging, and sidecar relationships. See [XMP sidecar
processing](sidecars.md) for sidecar field selection and sensitive field
exclusions.

## Environment Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `GOSYNC_VOLUME_PATH` | `./data` | Host path mounted to `/data` by `docker-compose.yml`. |
| `GOSYNC_WEB_PORT` | `49152` | Host port mapped to the container web UI by `docker-compose.yml`. |
| `DATA_DIR` | `/data` | Container path containing the HAR file, downloads, sidecars, metadata dumps, state, and reports. |
| `HAR_FILE` | `gopro.com.har` | HAR filename inside `DATA_DIR`. Paths and non-`.har` filenames are rejected. HAR method only. |
| `AUTH_METHOD` | `har` | `har` (default) or `api_token`. |
| `AUTH_TOKEN` | *(empty)* | GoPro bearer token captured from a DevTools request. Required when `AUTH_METHOD=api_token`. Treat like a password. |
| `USER_ID` | *(empty)* | Optional GoPro account/user id, used only to scope `api_token`-method media searches. |
| `DOWNLOAD_TELEMETRY` | `false` | Also fetch per-item `mediainfo.json` and GPX/GPMF telemetry sidecars. Adds two extra live API calls per item. |
| `CREATE_XMP_SIDECARS` | `true` | Generate Immich-compatible XMP sidecar files next to each downloaded media file. |
| `DOWNLOAD_FOLDER` | `downloads` | Download output folder. Relative paths are resolved inside `DATA_DIR`. |
| `GOSYNC_STATE_FILE` | `gosync_state.json` | JSON state file for all parsed media items. Relative paths are resolved inside `DATA_DIR`. |
| `BATCH_MAX_BYTES` | `auto` | Requested maximum total source bytes per zip batch. Values above the largest known `file_size` are capped to that largest file size. |
| `REQUEST_TIMEOUT_SECONDS` | `60` | HTTP request timeout for GoPro API calls. |
| `FFMPEG_BINARY` | `ffmpeg` | ffmpeg binary name or path used to merge GoPro chapter files. |
| `FFMPEG_TIMEOUT_SECONDS` | `900` | Timeout in seconds for each ffmpeg chapter-merge subprocess call. |
| `EXIFTOOL_BINARY` | `exiftool` | exiftool binary name or path used to copy camera/lens metadata onto merged chapter files. |
| `EXIFTOOL_TIMEOUT_SECONDS` | `300` | Timeout in seconds for each exiftool metadata-copy subprocess call. |
| `ENV` | `production` | Runtime environment. Set to `dev` or `development` to enable Flask debug mode. |
| `WEB_HOST` | `0.0.0.0` | Container bind host for the web server. |
| `WEB_PORT` | `8080` | Container port for the web server. |
| `ACCESS_LOGS` | `true` | Set to `false` to hide Flask access logs, including `/status` polling. |

Example:

```bash
docker run --rm \
  -p 8080:8080 \
  -e DOWNLOAD_FOLDER=media \
  -e BATCH_MAX_BYTES=2147483648 \
  -v "$PWD/data:/data" \
  ghcr.io/prabhanshuattri/gosync:latest
```

See [media items](media-items.md) for the media data model, HAR/API manifest
records, and chapter merging, [XMP sidecar processing](sidecars.md) for
sidecar field selection, and [development.md](development.md) for local
build and test commands.
