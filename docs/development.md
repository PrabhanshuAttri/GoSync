# Development Guide

Use this guide when building, testing, or running GoSync from source.

## Local Docker Build

Build and run the image locally when developing or testing changes:

```bash
docker build -t gosync:local .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" gosync:local
```

Open `http://localhost:8080` after the container starts.

## Direct Python Run

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

Run the downloader once without the web UI:

```bash
PYTHONPATH=src python -m gosync --data-dir ./data --run-once
```

Print the app version:

```bash
PYTHONPATH=src python -m gosync --version
```

## Tests

Run the full test suite:

```bash
python -m pytest
```

Run a focused test module:

```bash
python -m pytest tests/unit/test_downloader.py
```

## Releases

Release and publishing steps are documented in [release.md](release.md).
