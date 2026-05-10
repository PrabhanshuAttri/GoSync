import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from gosync.constants import STATUS_DOWNLOADED, STATUS_FAILED, STATUS_PENDING
from gosync.manifest import MediaManifest
from gosync.paths import media_download_path

STATE_VERSION = 1


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"State file must contain a JSON object: {path}")
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = _now()
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def create_or_update_state(
    state_file: Path,
    manifest: MediaManifest,
) -> dict[str, Any]:
    existing = _read_json(state_file)
    existing_media = existing.get("media", {})
    if not isinstance(existing_media, dict):
        existing_media = {}

    media_records: dict[str, dict[str, Any]] = {}
    for item in manifest.media:
        previous = existing_media.get(item.key, {})
        if not isinstance(previous, dict):
            previous = {}

        download_status = str(previous.get("download_status") or STATUS_PENDING)

        media_records[item.key] = {
            "key": item.key,
            "id": item.media_id,
            "filename": item.filename,
            "sidecar_filename": item.sidecar_filename,
            "file_size": item.file_size,
            "download_status": download_status,
            "sidecar_status": str(previous.get("sidecar_status") or STATUS_PENDING),
            "retry_count": int(previous.get("retry_count") or 0),
            "last_error": str(previous.get("last_error") or ""),
        }

    payload = {
        "version": STATE_VERSION,
        "created_at": existing.get("created_at") or _now(),
        "updated_at": existing.get("updated_at") or "",
        "media": media_records,
    }
    _write_json(state_file, payload)
    return payload


def load_state(state_file: Path) -> dict[str, Any]:
    payload = _read_json(state_file)
    if not payload:
        return {
            "version": STATE_VERSION,
            "created_at": _now(),
            "updated_at": "",
            "media": {},
        }
    return payload


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    _write_json(state_file, state)


def downloaded_filename_index(output_dir: Path) -> set[str]:
    if not output_dir.exists():
        return set()
    return {
        path.name.casefold()
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() != ".xmp"
    }


def sync_state_with_downloads(
    state_file: Path,
    output_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    state = load_state(state_file)
    media = state.get("media", {})
    if not isinstance(media, dict):
        media = {}
        state["media"] = media

    changed: list[dict[str, str]] = []
    existing_filenames = downloaded_filename_index(output_dir)
    for record in media.values():
        if not isinstance(record, dict):
            continue
        filename = str(record.get("filename") or "")
        if not filename:
            continue

        exists = (
            media_download_path(output_dir, filename).is_file()
            or (output_dir / filename).is_file()
            or filename.casefold() in existing_filenames
        )
        current_status = str(record.get("download_status") or STATUS_PENDING)
        if exists and current_status != STATUS_DOWNLOADED:
            record["download_status"] = STATUS_DOWNLOADED
            record["last_error"] = ""
            changed.append(
                {
                    "id": str(record.get("id") or ""),
                    "filename": filename,
                    "status": "found",
                }
            )
        elif not exists and current_status == STATUS_DOWNLOADED:
            record["download_status"] = STATUS_PENDING
            changed.append(
                {
                    "id": str(record.get("id") or ""),
                    "filename": filename,
                    "status": "missing",
                }
            )

    if changed:
        save_state(state_file, state)
    return state, changed


def media_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    media = state.get("media", {})
    if not isinstance(media, dict):
        return []
    return [record for record in media.values() if isinstance(record, dict)]


def completed_count(state: dict[str, Any]) -> int:
    return sum(
        1
        for record in media_records(state)
        if record.get("download_status") == STATUS_DOWNLOADED
    )


def downloaded_extension_counts(state: dict[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in media_records(state):
        if record.get("download_status") != STATUS_DOWNLOADED:
            continue
        filename = str(record.get("filename") or "")
        extension = Path(filename).suffix.lower().lstrip(".") or "no_extension"
        counts[extension] += 1
    return dict(sorted(counts.items()))


def format_downloaded_extension_summary(state: dict[str, Any]) -> str:
    counts = downloaded_extension_counts(state)
    total = sum(counts.values())
    if not counts:
        return "Already downloaded by extension: none (0 files)"

    details = ", ".join(
        f"{extension.upper()}: {count}" for extension, count in counts.items()
    )
    file_label = "file" if total == 1 else "files"
    return f"Already downloaded by extension: {details} ({total} {file_label})"


def pending_keys(state: dict[str, Any]) -> set[str]:
    return {
        str(record.get("key"))
        for record in media_records(state)
        if record.get("download_status") != STATUS_DOWNLOADED
    }


def mark_sidecars(
    state_file: Path,
    keys: list[str],
    status: str,
    error: str = "",
) -> None:
    state = load_state(state_file)
    media = state.get("media", {})
    if not isinstance(media, dict):
        return
    for key in keys:
        record = media.get(key)
        if isinstance(record, dict):
            record["sidecar_status"] = status
            record["last_error"] = error
    save_state(state_file, state)


def mark_downloaded(state_file: Path, keys: list[str]) -> dict[str, Any]:
    state = load_state(state_file)
    media = state.get("media", {})
    if not isinstance(media, dict):
        return state
    for key in keys:
        record = media.get(key)
        if isinstance(record, dict):
            record["download_status"] = STATUS_DOWNLOADED
            record["last_error"] = ""
    save_state(state_file, state)
    return state


def mark_failed(
    state_file: Path,
    keys: list[str],
    error: str,
    *,
    retry: bool = True,
) -> dict[str, Any]:
    state = load_state(state_file)
    media = state.get("media", {})
    if not isinstance(media, dict):
        return state
    for key in keys:
        record = media.get(key)
        if isinstance(record, dict):
            record["last_error"] = error
            if retry:
                record["retry_count"] = int(record.get("retry_count") or 0) + 1
            else:
                record["download_status"] = STATUS_FAILED
    save_state(state_file, state)
    return state
