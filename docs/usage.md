# Usage Guide

This guide covers the day-to-day GoSync workflow: using the web UI, resuming an
interrupted run, understanding the data directory, and configuring the
container.

## Web UI Workflow

The web UI is the default container experience. It is a local dashboard for
managing one download job at a time.

1. Open `http://localhost:49152` when using Docker Compose, or the host port you
   mapped with Docker.
2. In **HAR File**, upload the HAR export from your logged-in GoPro session.
3. In **Download Job**, select the uploaded HAR file and choose **Files per
   batch** when you want the web UI to limit how many selected files are placed
   in each batch.
4. In **Media Files**, select the pending files you want to download. Downloaded
   rows are disabled. You can filter by extension and sort by size or status.
5. Click **Start Download**. The browser keeps selected files, sort order, and
   files-per-batch through the start redirect.
6. Click **Stop Download** if you need to cancel the active job. GoSync stops
   safely, removes the temporary zip, and keeps JSON state for resume.
7. Watch **Media Complete**, **Downloaded**, **Batches**, and transfer progress
   update live.
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

If the container stops or your network drops, start GoSync again with the same
data directory. The web UI syncs `gosync_state.json` against the actual files in
`downloads/`, marks missing files pending again, and skips files that are
already present.

GoSync downloads media in size-based batches. With the default
`BATCH_MAX_BYTES=auto`, the batch cap is the largest `file_size` found in the
HAR manifest. Explicit `BATCH_MAX_BYTES` values above the largest known file are
capped to that largest file size, so a batch should not be larger than the
largest individual media file. When the web UI downloads only selected files,
the automatic byte cap still comes from the full HAR manifest, while the
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
|-- reports/
|   `-- gosync-report-20260501-120000.json
`-- downloads/
    |-- jpg/
    |   |-- example-photo.JPG
    |   `-- example-photo.JPG.xmp
    `-- mp4/
        |-- example-video.MP4
        `-- example-video.MP4.xmp
```

Files and folders:

- `gopro.com.har`: exported browser HAR file, or another filename set with
  `HAR_FILE`.
- `downloads/`: downloaded media output folder, configurable with
  `DOWNLOAD_FOLDER`. Media files and their XMP sidecars are grouped together in
  extension folders such as `downloads/mp4/` and `downloads/jpg/`.
- `manifest.json`: media manifest parsed from HAR `media/search` responses.
- `media_search.json`: dump of all media objects extracted from HAR
  `media/search` responses, including duplicates before manifest
  deduplication.
- `gosync_state.json`: JSON resume state for every parsed media item,
  configurable with `GOSYNC_STATE_FILE`.
- `reports/`: run reports written when downloads complete or stop.
- `gopro_temp_batch.zip`: temporary zip file used during a batch download;
  deleted after extraction or failure.

See [download items](download-items.md) for HAR-derived media records,
extraction behavior, and sidecar relationships. See [XMP sidecar
processing](sidecars.md) for sidecar field selection and sensitive field
exclusions.

## Environment Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `GOSYNC_VOLUME_PATH` | `./data` | Host path mounted to `/data` by `docker-compose.yml`. |
| `GOSYNC_WEB_PORT` | `49152` | Host port mapped to the container web UI by `docker-compose.yml`. |
| `DATA_DIR` | `/data` | Container path containing the HAR file, downloads, sidecars, metadata dumps, state, and reports. |
| `HAR_FILE` | `gopro.com.har` | HAR filename inside `DATA_DIR`. Paths and non-`.har` filenames are rejected. |
| `DOWNLOAD_FOLDER` | `downloads` | Download output folder. Relative paths are resolved inside `DATA_DIR`. |
| `GOSYNC_STATE_FILE` | `gosync_state.json` | JSON state file for all parsed media items. Relative paths are resolved inside `DATA_DIR`. |
| `BATCH_MAX_BYTES` | `auto` | Requested maximum total source bytes per zip batch. Values above the largest `file_size` in the HAR manifest are capped to that largest file size. |
| `REQUEST_TIMEOUT_SECONDS` | `60` | HTTP request timeout for GoPro API calls. |
| `FFMPEG_BINARY` | `ffmpeg` | ffmpeg binary name or path used to merge GoPro chapter files. |
| `FFMPEG_TIMEOUT_SECONDS` | `900` | Timeout in seconds for each ffmpeg chapter-merge subprocess call. |
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
  ghcr.io/prabhanshuattri/gosync:1.3.0
```
