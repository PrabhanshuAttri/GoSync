import base64
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from gosync.config import REQUEST_TIMEOUT
from gosync.constants import (
    API_JSON_ACCEPT,
    API_JSON_USER_AGENT,
    API_MEDIA_SEARCH_FIELDS,
    API_MEDIA_SEARCH_PER_PAGE,
    MEDIA_LIST_KEYS,
    MEDIA_SEARCH_URL,
)

LOGGER = logging.getLogger("gosync.manifest")


@dataclass(frozen=True)
class MediaItem:
    key: str
    media_id: str
    filename: str
    sidecar_filename: str
    file_size: int | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DuplicateMediaItem:
    key: str
    media_id: str
    filename: str


@dataclass(frozen=True)
class MediaManifest:
    media: list[MediaItem]
    duplicates: list[DuplicateMediaItem]
    matching_entries: int
    media_responses: list[dict[str, Any]]


def media_key(media_id: str, filename: str) -> str:
    return f"{media_id}_{filename}"


def sidecar_stem(metadata: dict[str, Any]) -> str:
    filename = Path(str(metadata["filename"])).name
    extension = str(metadata.get("file_extension") or Path(filename).suffix).lstrip(".")

    if not extension:
        return filename

    suffix = f".{extension.lower()}"
    if filename.lower().endswith(suffix):
        return filename

    return f"{filename}.{extension}"


def sidecar_filename(metadata: dict[str, Any]) -> str:
    return f"{sidecar_stem(metadata)}.xmp"


def filename_extension(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    return extension or "no extension"


def extension_counts(manifest: MediaManifest) -> dict[str, int]:
    counts = Counter(filename_extension(item.filename) for item in manifest.media)
    return dict(sorted(counts.items()))


def format_extension_summary(manifest: MediaManifest) -> str:
    counts = extension_counts(manifest)
    if not counts:
        return "No media files found."

    summary = ", ".join(
        f"{extension.upper()}: {count}" for extension, count in counts.items()
    )
    return f"HAR Media by extension: {summary}"


def parse_response_text(text: str, entry_number: int) -> Any | None:
    if not text:
        return None

    candidates = [text.strip()]
    if text.lstrip().startswith(")]}',"):
        candidates.append(text.split("\n", 1)[-1].strip())

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    try:
        decoded = base64.b64decode(text).decode("utf-8", errors="ignore").strip()
        return json.loads(decoded)
    except Exception:
        LOGGER.warning("Skipped unparsable media/search response #%s", entry_number)
        return None


def is_media_file(item: Any) -> bool:
    if not isinstance(item, dict):
        return False

    filename = item.get("filename")
    extension = item.get("file_extension") or Path(
        str(filename or "")
    ).suffix.lstrip(".")
    if not filename or not extension:
        return False

    # Skip JSON files
    if extension.lower() == "json":
        return False

    content_type = str(item.get("content_type", "")).lower()
    item_type = str(item.get("type", "")).lower()
    non_media_types = {
        "album",
        "folder",
        "page",
        "pagination",
        "profile",
        "summary",
    }
    return item_type not in non_media_types and content_type not in non_media_types


def iter_candidate_lists(response_json: Any) -> list[list[Any]]:
    lists: list[list[Any]] = []
    if not isinstance(response_json, dict):
        return lists

    embedded = response_json.get("_embedded")
    if isinstance(embedded, dict):
        for key in MEDIA_LIST_KEYS:
            value = embedded.get(key)
            if isinstance(value, list):
                lists.append(value)

    for key in MEDIA_LIST_KEYS:
        value = response_json.get(key)
        if isinstance(value, list):
            lists.append(value)

    return lists


def collect_media_items(obj: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        if is_media_file(obj):
            found.append(obj)
            return
        for value in obj.values():
            collect_media_items(value, found)
    elif isinstance(obj, list):
        for item in obj:
            collect_media_items(item, found)


def extract_media_items(response_json: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for candidate_list in iter_candidate_lists(response_json):
        for item in candidate_list:
            if isinstance(item, dict):
                items.append(item)

    if not items:
        collect_media_items(response_json, items)

    return items


def _file_size(value: Any) -> int | None:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _unnamed_filename(index: int, metadata: dict[str, Any]) -> str:
    filename = f"unnamed_{index}"
    extension = str(metadata.get("file_extension") or "").strip().lstrip(".")
    if extension:
        return f"{filename}.{extension}"
    return filename


def _to_media_item(
    metadata: dict[str, Any],
    unnamed_index: int,
) -> tuple[MediaItem | None, bool]:
    media_id = str(metadata.get("id") or "").strip()
    original_filename = str(metadata.get("filename") or "").strip()
    generated_filename = not original_filename
    filename = Path(original_filename).name if original_filename else ""
    if not media_id:
        return None, False
    if not filename:
        # GoPro media records should still provide an ID; only synthesize the
        # missing filename so state, sidecars, and reports have a stable label.
        filename = _unnamed_filename(unnamed_index, metadata)

    normalized_metadata = dict(metadata)
    normalized_metadata["filename"] = filename
    key = media_key(media_id, filename)
    return (
        MediaItem(
            key=key,
            media_id=media_id,
            filename=filename,
            sidecar_filename=sidecar_filename(normalized_metadata),
            file_size=_file_size(normalized_metadata.get("file_size")),
            metadata=normalized_metadata,
        ),
        generated_filename,
    )


def build_manifest_from_pages(
    pages: list[tuple[str, list[dict[str, Any]]]],
    matching_entries: int | None = None,
) -> MediaManifest:
    """Build a deduplicated MediaManifest from (request_url, raw_media_dicts)
    pages. Source-agnostic: pages may come from parsing a HAR file or from
    paginating the live /media/search API -- both yield the same GoPro
    response shape."""
    media_items: list[MediaItem] = []
    duplicates: list[DuplicateMediaItem] = []
    media_responses: list[dict[str, Any]] = []
    seen: set[str] = set()
    unnamed_count = 0

    for entry_number, (url, raw_items) in enumerate(pages, start=1):
        response_media: list[dict[str, Any]] = []
        for metadata in raw_items:
            media_item, generated_filename = _to_media_item(
                metadata,
                unnamed_count + 1,
            )
            if not media_item:
                continue
            # Skip JSON files at manifest creation level
            if media_item.filename.lower().endswith('.json'):
                continue
            if generated_filename:
                unnamed_count += 1
                normalized = dict(metadata)
                normalized["filename"] = media_item.filename
                response_media.append(normalized)
            else:
                response_media.append(metadata)

            if media_item.key in seen:
                duplicates.append(
                    DuplicateMediaItem(
                        key=media_item.key,
                        media_id=media_item.media_id,
                        filename=media_item.filename,
                    )
                )
                continue
            seen.add(media_item.key)
            media_items.append(media_item)

        media_responses.append(
            {
                "entry_number": entry_number,
                "request_url": str(url),
                "media_count": len(response_media),
                "media": response_media,
            }
        )

    if not media_items:
        raise ValueError("No media file metadata found in media/search responses")

    return MediaManifest(
        media=media_items,
        duplicates=duplicates,
        matching_entries=len(pages) if matching_entries is None else matching_entries,
        media_responses=media_responses,
    )


def _pages_from_har(
    har_path: Path,
) -> tuple[list[tuple[str, list[dict[str, Any]]]], int]:
    try:
        with har_path.open("r", encoding="utf-8", errors="ignore") as file:
            har_data = json.load(file)
    except FileNotFoundError:
        raise FileNotFoundError(f"HAR file not found: {har_path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse HAR file as JSON: {exc}") from exc

    entries = har_data.get("log", {}).get("entries")
    if not isinstance(entries, list):
        raise ValueError("Invalid HAR file structure: missing log.entries")

    pages: list[tuple[str, list[dict[str, Any]]]] = []
    matching_entries = 0

    for entry in entries:
        request = entry.get("request", {})
        url = request.get("url", "")
        if MEDIA_SEARCH_URL not in url:
            continue

        matching_entries += 1
        text = entry.get("response", {}).get("content", {}).get("text", "")
        response_json = parse_response_text(text, matching_entries)
        if response_json is None:
            continue

        pages.append((str(url), extract_media_items(response_json)))

    if matching_entries == 0:
        raise ValueError(f"No API calls to {MEDIA_SEARCH_URL} found in HAR file")

    return pages, matching_entries


def read_manifest_from_har(har_path: Path) -> MediaManifest:
    pages, matching_entries = _pages_from_har(har_path)
    return build_manifest_from_pages(pages, matching_entries=matching_entries)


def json_api_headers(headers: dict[str, str]) -> dict[str, str]:
    """Adapt the pipeline's request headers for GoPro's JSON media API
    (media/search, media/{id}, media/{id}/download).

    Keeps whatever Authorization/Cookie step 1 resolved -- from a HAR capture
    or a directly-provided bearer token, either way -- and only overrides
    the Accept/Content-Type/User-Agent fields to the values confirmed working
    against these JSON endpoints, since the zip-export endpoint (what the
    rest of the pipeline's `headers` dict is otherwise tuned for) expects a
    different Accept.
    """
    return {
        **headers,
        "Accept": API_JSON_ACCEPT,
        "Content-Type": "application/json",
        "User-Agent": API_JSON_USER_AGENT,
    }


def _pages_from_live_api(
    headers: dict[str, str],
    user_id: str | None = None,
    per_page: int = API_MEDIA_SEARCH_PER_PAGE,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Paginate GoPro's live /media/search endpoint for the whole account."""
    pages: list[tuple[str, list[dict[str, Any]]]] = []
    page_number = 1
    request_headers = json_api_headers(headers)

    while True:
        params: dict[str, str | int] = {
            "fields": API_MEDIA_SEARCH_FIELDS,
            "page": page_number,
            "per_page": per_page,
        }
        if user_id:
            params["gopro_user_id"] = user_id

        response = requests.get(
            MEDIA_SEARCH_URL,
            headers=request_headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        response_json = response.json()
        pages.append((response.url, extract_media_items(response_json)))

        total_pages = response_json.get("_pages", {}).get("total_pages", page_number)
        if page_number >= total_pages:
            break
        page_number += 1

    return pages


def read_manifest_from_api(
    headers: dict[str, str],
    user_id: str | None = None,
) -> MediaManifest:
    pages = _pages_from_live_api(headers, user_id)
    if not pages:
        raise ValueError(f"No responses from {MEDIA_SEARCH_URL}")
    return build_manifest_from_pages(pages)


def write_manifest(
    manifest: MediaManifest,
    manifest_file: Path,
    har_path: Path,
) -> None:
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_har": str(har_path),
        "matching_entries": manifest.matching_entries,
        "media_count": len(manifest.media),
        "duplicate_count": len(manifest.duplicates),
        "media": [asdict(item) for item in manifest.media],
        "duplicates": [asdict(item) for item in manifest.duplicates],
    }
    manifest_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_media_responses_dump(
    manifest: MediaManifest,
    dump_file: Path,
    har_path: Path,
) -> None:
    dump_file.parent.mkdir(parents=True, exist_ok=True)
    all_media: list[dict[str, Any]] = []
    for response in manifest.media_responses:
        media = response.get("media", [])
        if isinstance(media, list):
            all_media.extend(item for item in media if isinstance(item, dict))

    payload = {
        "source_har": str(har_path),
        "request_url": MEDIA_SEARCH_URL,
        "matching_responses": manifest.matching_entries,
        "media_count": len(all_media),
        "media": all_media,
    }
    dump_file.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
