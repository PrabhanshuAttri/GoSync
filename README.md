# GoSync

GoSync is a self-hosted Docker utility for downloading a GoPro cloud media library when the official bulk download flow is too limited for large collections.

GoPro Cloud limits bulk downloads to 25 items at a time. That becomes painful if you have hundreds or thousands of photos and videos, because you have to manually select, download, track, and retry many small batches. Large browser downloads can also fail partway through, leaving you unsure what finished and what still needs to be downloaded.

GoSync works around that workflow by reading media metadata and session headers from a browser HAR export, calling the GoPro download API directly, downloading in safe batches, extracting each batch into a mounted data directory, and maintaining JSON state so interrupted runs can resume. The container includes a basic web UI for uploading the HAR file, starting the download, and watching live progress.

This project was inspired by [josefkeup741/gopro-cloud-rescue](https://github.com/josefkeup741/gopro-cloud-rescue).

GoSync is a vibe-coded, AI-assisted utility built around a real GoPro Cloud download need.

Published image:

```text
ghcr.io/prabhanshuattri/gosync:1.0.3
```

Current app version:

```text
1.0.3
```

## Screenshot

![GoSync web dashboard](docs/assets/gosync-dashboard.png)

## Requirements

- Docker or Docker Compose
- A HAR file exported from your logged-in GoPro media library
- Enough free disk space in the mounted data directory for the downloaded media

## Export The HAR File

1. Log in to your GoPro media library in a browser.
2. Open Developer Tools with `F12`.
3. Go to the Network tab.
4. Refresh the page while the Network tab is recording.
5. Slowly scroll to the bottom of the GoPro media library so every item loads.
6. Export the Network log as a HAR file.
7. Save the file as `gopro.com.har`, or use a custom name and set `HAR_FILE`.

HAR files can contain sensitive session data such as cookies or authorization headers. Keep the file private and delete it when you no longer need it.

GoSync reuses GoPro session headers from the HAR file. If downloads fail with `403 Forbidden`, export a fresh HAR while logged in and make sure the Network export includes request headers such as `Cookie` or `Authorization`.

## Run The Project

Create a data directory:

```bash
mkdir -p data
```

Run with Docker Compose:

```bash
docker compose up
```

Open the web UI:

```text
http://localhost:49152
```

The default [docker-compose.yml](docker-compose.yml) pulls the versioned published image, mounts `./data` into the container at `/data`, and exposes the web UI on host port `49152`.

To use a different host directory or web port:

```bash
GOSYNC_VOLUME_PATH=/mnt/media/gosync GOSYNC_WEB_PORT=49153 docker compose up
```

Run with plain Docker:

```bash
docker run --rm \
  -p 8080:8080 \
  -v "$PWD/data:/data" \
  ghcr.io/prabhanshuattri/gosync:1.0.3
```

Then open `http://localhost:8080`.

## Using The Web UI

The web UI is the default container experience. It is a small local dashboard for managing one download job at a time.

1. Open `http://localhost:49152` when using Docker Compose, or the host port you mapped with Docker.
2. In **HAR File**, upload the HAR export from your logged-in GoPro session.
3. In **Download Job**, select the uploaded HAR file and click **Start Download**.
4. Click **Stop Download** if you need to cancel the active job. GoSync stops safely, removes the temporary zip, and keeps JSON state for resume.
5. Watch **Media**, **Batches**, and **Current Download** progress update live.
6. Use **Current Activity** for the latest detailed action, such as the batch currently being processed.
7. Use **Media Files** to see every parsed media item. The table can filter by extension and sorts current downloads first, then downloaded files, then pending files.
8. Use **Event Log** for a running history of uploads, extension summaries, retries, stops, failures, completed batches, and final run summaries.
9. Use the **Light** or **Dark** toggle in the header to switch themes. The browser remembers your choice.

The dashboard stores uploaded HAR files in the mounted data directory. Downloaded media, generated sidecars, media metadata dumps, JSON state, run reports, and the temporary batch zip file also live in that same directory.

If the container stops or your network drops, start GoSync again with the same data directory. The web UI will sync `gosync_state.json` against the actual files in `downloads/` and resume anything still pending.

## Data Directory Structure

The mounted data directory is the only persistent storage GoSync needs. By default it looks like this:

```text
data/
├── gopro.com.har
├── manifest.json
├── media_search.json
├── gosync_state.json
├── reports/
│   └── gosync-report-20260501-120000.json
└── downloads/
    ├── jpg/
    │   ├── GX010002.JPG
    │   └── GX010002.JPG.xmp
    └── mp4/
        ├── GX010001.MP4
        └── GX010001.MP4.xmp
```

Files and folders:

- `gopro.com.har`: exported browser HAR file, or another filename set with `HAR_FILE`.
- `downloads/`: downloaded media output folder, configurable with `DOWNLOAD_FOLDER`.
  Media files and their XMP sidecars are grouped together in extension folders
  such as `downloads/mp4/` and `downloads/jpg/`.
- `manifest.json`: media manifest parsed from HAR `media/search` responses.
- `media_search.json`: dump of all media objects extracted from HAR
  `media/search` responses, including duplicates before manifest deduplication.
- `gosync_state.json`: JSON resume state for every parsed media item, configurable with `GOSYNC_STATE_FILE`.
- `reports/`: run reports written when downloads complete or stop.
- `completed_ids.txt`: legacy resume ledger imported once for backward compatibility.
- `gopro_temp_batch.zip`: temporary zip file used during a batch download; deleted after extraction or failure.

See [XMP sidecar processing](docs/sidecars.md) for sidecar field selection
and sensitive field exclusions.

If a run is interrupted, start the container again with the same data directory. GoSync syncs `gosync_state.json` to the files in `downloads/`, marks missing files pending again, and skips files that are already present.

## Local Build

Build and run the image locally when developing or testing changes:

```bash
docker build -t gosync:local .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" gosync:local
```

Run the Python web UI directly:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m gosync --data-dir ./data
```

Flask debug mode is disabled by default, including for local direct runs. Set
`ENV=dev` only when actively developing:

```bash
ENV=dev PYTHONPATH=src python -m gosync --data-dir ./data
```

Print the app version:

```bash
PYTHONPATH=src python -m gosync --version
```

Release and publishing steps are documented in [docs/release.md](docs/release.md).

Run the downloader once without the web UI:

```bash
PYTHONPATH=src python -m gosync --data-dir ./data --run-once
```

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
| `BATCH_MAX_BYTES` | `auto` | Maximum total source bytes per zip batch. `auto` uses the largest `file_size` found in the HAR manifest. |
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
  ghcr.io/prabhanshuattri/gosync:1.0.3
```
