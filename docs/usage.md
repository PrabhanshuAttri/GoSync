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
3. In **Download Job**, select the uploaded HAR file and click **Start
   Download**.
4. Click **Stop Download** if you need to cancel the active job. GoSync stops
   safely, removes the temporary zip, and keeps JSON state for resume.
5. Watch **Media**, **Batches**, and **Current Download** progress update live.
6. Use **Current Activity** for the latest detailed action, such as the batch
   currently being processed.
7. Use **Media Files** to see every parsed media item. The table can filter by
   extension and sorts current downloads first, then downloaded files, then
   pending files.
8. Use **Event Log** for a running history of uploads, extension summaries,
   retries, stops, failures, completed batches, and final run summaries.
9. Use the **Light** or **Dark** toggle in the header to switch themes. The
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
largest individual media file.

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
    |   |-- GX010002.JPG
    |   `-- GX010002.JPG.xmp
    `-- mp4/
        |-- GX010001.MP4
        `-- GX010001.MP4.xmp
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
- `completed_ids.txt`: legacy resume ledger imported once for backward
  compatibility.
- `gopro_temp_batch.zip`: temporary zip file used during a batch download;
  deleted after extraction or failure.

See [XMP sidecar processing](sidecars.md) for sidecar field selection and
sensitive field exclusions.

## Environment Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `GOSYNC_VOLUME_PATH` | `./data` | Host path mounted to `/data` by `docker-compose.yml`. |
| `GOSYNC_WEB_PORT` | `49152` | Host port mapped to the container web UI by `docker-compose.yml`. |
| `DATA_DIR` | `/data` | Container path containing the HAR file, downloads, sidecars, metadata dumps, state, and reports. |
| `HAR_FILE` | `gopro.com.har` | HAR filename or path. Relative paths are resolved inside `DATA_DIR`. |
| `DOWNLOAD_FOLDER` | `downloads` | Download output folder. Relative paths are resolved inside `DATA_DIR`. |
| `SIDECAR_FOLDER` | `sidecars` | Deprecated. XMP sidecars are written next to media files inside `DOWNLOAD_FOLDER`. |
| `GOSYNC_STATE_FILE` | `gosync_state.json` | JSON state file for all parsed media items. Relative paths are resolved inside `DATA_DIR`. |
| `COMPLETED_LOG` | `completed_ids.txt` | Legacy resume ledger imported once for backward compatibility. |
| `BATCH_MAX_BYTES` | `auto` | Requested maximum total source bytes per zip batch. Values above the largest `file_size` in the HAR manifest are capped to that largest file size. |
| `BATCH_SIZE` | `5` | Deprecated; retained for compatibility but not used by size-based batching. |
| `MAX_RETRY_PASSES` | `3` | Deprecated; multi-file failures split into one-file retries, and single-file batches retry up to 3 times. |
| `REQUEST_TIMEOUT_SECONDS` | `60` | HTTP request timeout for GoPro API calls. |
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
  ghcr.io/prabhanshuattri/gosync:1.1.0
```
