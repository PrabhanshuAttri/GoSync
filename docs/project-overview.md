# Project Overview

GoSync is a self-hosted Python and Docker utility for downloading GoPro Cloud
media libraries from a browser HAR export. It provides a web UI for selecting
pending media, running resumable batch downloads, monitoring progress, and
writing downloaded media plus XMP sidecars into a mounted data directory.

## Key Capabilities

- Parses GoPro `media/search` responses from HAR files into a local manifest.
- Reuses browser session headers from the HAR export for GoPro API downloads.
- Downloads pending media in resumable size-based batches.
- Supports optional files-per-batch limits from the web UI.
- Tracks durable download state in `gosync_state.json`.
- Generates XMP sidecar files next to downloaded media.
- Provides grouped status events, transfer progress, notifications, and run
  reports.

## Runtime Components

- `app.py`: application entry point for the web server or one-shot CLI run.
- `config.py`: environment variables, defaults, and argument parsing.
- `constants.py`: shared filenames, status values, and API defaults.
- `manifest.py`: HAR parsing, media extraction, deduplication, and manifest
  writing.
- `downloader.py`: browser-header extraction, batching, download streaming,
  zip validation, extraction, retry behavior, and download events.
- `state.py`: JSON state creation, resume sync, download status, retry count,
  and sidecar status updates.
- `progress.py`: in-memory web job state, event normalization, and
  notifications.
- `events.py`: structured event creation, redaction, UI presentation metadata,
  and CLI log formatting.
- `runtime.py`: path preparation, job orchestration, run reports, and CLI
  run-once flow.
- `sidecar.py`: XMP sidecar generation from parsed media metadata.
- `web.py`: Flask routes for the dashboard, uploads, job control, status,
  current-run events, and media rows.

## Data Flow

1. A HAR file is uploaded or selected from the data directory.
2. GoSync parses media metadata and writes `manifest.json`,
   `media_search.json`, and `gosync_state.json`.
3. The web UI starts a job with selected pending media.
4. GoSync extracts browser session headers from the HAR.
5. Pending media are grouped into batches and downloaded as temporary zip files.
6. Each zip is validated, extracted, organized by extension, and then marked
   downloaded in JSON state.
7. XMP sidecars are generated next to media files.
8. Completion, stop, or failure reports are written under `reports/`.

See [usage.md](usage.md) for the user workflow and [development.md](development.md)
for local build and test commands.
