import json
import logging
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests

from gosync.config import REQUEST_TIMEOUT
from gosync.constants import MEDIA_DOWNLOAD_URL, STATUS_COMPLETE, STATUS_FAILED
from gosync.manifest import MediaItem, json_api_headers
from gosync.paths import safe_filename
from gosync.progress import ProgressState
from gosync.state import load_state, mark_telemetry

LOGGER = logging.getLogger("gosync.telemetry")

GPX_NS = "http://www.topografix.com/GPX/1/1"
ET.register_namespace("", GPX_NS)

# Account-identifying / access fields -- not useful metadata about the file
# itself, so strip them before writing anything to disk.
SENSITIVE_MEDIA_FIELDS = {"token", "user_id", "gopro_user_id"}


def media_stem(filename: str) -> str:
    return Path(safe_filename(filename)).stem


def telemetry_output_path(output_dir: Path, extension: str, stem_suffix: str) -> Path:
    """Telemetry files are grouped by their own extension under output_dir,
    the same convention media_download_path uses for downloaded media --
    e.g. downloads/gpmf/, downloads/json/ -- rather than living next to the
    source media file."""
    extension_dir = output_dir / extension
    extension_dir.mkdir(parents=True, exist_ok=True)
    return extension_dir / f"{stem_suffix}.{extension}"


def merge_gpx(gpx_contents: list[bytes], name: str) -> bytes:
    """Combine multiple chapter GPX documents into one, one <trkseg> per chapter."""
    root = ET.fromstring(gpx_contents[0])
    trk = root.find(f"{{{GPX_NS}}}trk")

    for seg in trk.findall(f"{{{GPX_NS}}}trkseg"):
        trk.remove(seg)

    name_el = root.find(f"{{{GPX_NS}}}metadata/{{{GPX_NS}}}name")
    if name_el is not None:
        name_el.text = name

    for content in gpx_contents:
        chapter_trk = ET.fromstring(content).find(f"{{{GPX_NS}}}trk")
        for seg in chapter_trk.findall(f"{{{GPX_NS}}}trkseg"):
            trk.append(seg)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def first_gps_fix(gpx_contents: list[bytes]) -> tuple[float, float, float] | None:
    """Return (lat, lon, ele) of the first trackpoint with a real 3D GPS fix.

    Chapter 1 typically starts with placeholder points recorded before the
    camera acquires a GPS lock. Those points carry pdop=99.99 (GoPro's
    "no fix" sentinel) and no <fix> element -- including briefly *after*
    lat/lon go non-zero, where the receiver emits noisy garbage coordinates
    before it actually locks. A 2D fix only solves lat/lon, not altitude --
    only a 3D fix solves altitude too, so require a 3D fix specifically.
    """
    for content in gpx_contents:
        root = ET.fromstring(content)
        trk = root.find(f"{{{GPX_NS}}}trk")
        for seg in trk.findall(f"{{{GPX_NS}}}trkseg"):
            for trkpt in seg.findall(f"{{{GPX_NS}}}trkpt"):
                fix_el = trkpt.find(f"{{{GPX_NS}}}fix")
                if fix_el is None or fix_el.text != "3d":
                    continue
                lat = float(trkpt.get("lat"))
                lon = float(trkpt.get("lon"))
                ele_el = trkpt.find(f"{{{GPX_NS}}}ele")
                ele = float(ele_el.text) if ele_el is not None else 0.0
                return lat, lon, ele
    return None


def _get(url: str, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


def download_gpx_or_gpmf(
    download_data: dict[str, Any],
    output_dir: Path,
    stem: str,
) -> tuple[tuple[float, float, float] | None, str | None]:
    """Fetch GPX telemetry (merging chapters into one track), falling back to
    raw per-chapter GPMF sidecars when no GPX is available for this item.
    Written to downloads/gpx/ or downloads/gpmf/, grouped by extension the
    same way downloaded media is.

    Returns (geo, track_kind), where track_kind is "gpx", "gpmf", or None
    when the item has no GPS telemetry sidecar at all -- callers use it to
    report which track type was actually written for this item."""
    sidecar_files = download_data.get("_embedded", {}).get("sidecar_files", [])

    gpx_files = sorted(
        (f for f in sidecar_files if f.get("label") == "gpx"),
        key=lambda f: f.get("item_number", 1),
    )
    if gpx_files:
        gpx_contents = [_get(f["url"]).content for f in gpx_files]
        merged = merge_gpx(gpx_contents, stem)
        telemetry_output_path(output_dir, "gpx", stem).write_bytes(merged)
        return first_gps_fix(gpx_contents), "gpx"

    gpmf_files = sorted(
        (f for f in sidecar_files if f.get("label") == "gpmf"),
        key=lambda f: f.get("item_number", 1),
    )
    if not gpmf_files:
        return None, None

    for gpmf in gpmf_files:
        item_number = gpmf.get("item_number", 1)
        content = _get(gpmf["url"]).content
        out_path = telemetry_output_path(output_dir, "gpmf", f"{stem}_{item_number}")
        out_path.write_bytes(content)
    return None, "gpmf"


def download_mediainfo(
    headers: dict[str, str],
    media_id: str,
    download_data: dict[str, Any],
    output_dir: Path,
    stem: str,
    geo: tuple[float, float, float] | None = None,
) -> None:
    """Combine the /media/{id} record with the mediainfo sidecar (if any)
    into <stem>_mediainfo.json under downloads/json/, stripping
    account-identifying fields."""
    sidecar_files = download_data.get("_embedded", {}).get("sidecar_files", [])

    medium_data = _get(f"{MEDIA_DOWNLOAD_URL}/{media_id}", headers=headers).json()

    # folder_path embeds the same account UUID as gopro_user_id/user_id --
    # keep the path structure as a reference but redact the identifier.
    account_id = medium_data.get("gopro_user_id") or medium_data.get("user_id")
    if account_id and medium_data.get("folder_path"):
        medium_data["folder_path"] = medium_data["folder_path"].replace(
            account_id, "{user_id}"
        )

    for field in SENSITIVE_MEDIA_FIELDS:
        medium_data.pop(field, None)

    if geo:
        lat, lon, alt = geo
        medium_data["geoData"] = {"latitude": lat, "longitude": lon, "altitude": alt}
        medium_data["geoDataExif"] = dict(medium_data["geoData"])

    combined = {"media": medium_data, "mediainfo": None}

    mediainfo = next((f for f in sidecar_files if f.get("label") == "mediainfo"), None)
    if mediainfo:
        combined["mediainfo"] = _get(mediainfo["url"]).json()

    out_path = telemetry_output_path(output_dir, "json", f"{stem}_mediainfo")
    out_path.write_text(json.dumps(combined, indent=2), encoding="utf-8")


def fetch_item_telemetry(
    headers: dict[str, str],
    item: MediaItem,
    output_dir: Path,
) -> str | None:
    """Fetch this item's GPS track plus mediainfo.json. Returns the GPS track
    kind written ("gpx"/"gpmf"), or None if the item had no GPS telemetry --
    the mediainfo JSON is always written on success either way."""
    request_headers = json_api_headers(headers)
    download_data = _get(
        f"{MEDIA_DOWNLOAD_URL}/{item.media_id}/download",
        headers=request_headers,
    ).json()

    stem = media_stem(item.filename)
    geo, track_kind = download_gpx_or_gpmf(download_data, output_dir, stem)
    download_mediainfo(
        request_headers, item.media_id, download_data, output_dir, stem, geo
    )
    return track_kind


def _pending_items(
    media_items: list[MediaItem],
    state_file: Path | None,
) -> list[MediaItem]:
    if not state_file:
        return media_items
    media_state = load_state(state_file).get("media", {})
    if not isinstance(media_state, dict):
        return media_items
    return [
        item
        for item in media_items
        if not isinstance(media_state.get(item.key), dict)
        or media_state[item.key].get("telemetry_status") != STATUS_COMPLETE
    ]


def run_telemetry_job(
    headers: dict[str, str],
    media_items: list[MediaItem],
    output_dir: Path,
    state_file: Path | None = None,
    progress: ProgressState | None = None,
    job_id: str | None = None,
    force: bool = False,
) -> tuple[int, int]:
    """Fetch mediainfo.json + GPX/GPMF telemetry for each item. Returns
    (written_count, failed_count). A missing sidecar for a given item is
    expected, not an error -- only network/HTTP failures count as failed.
    force=True re-fetches every item, bypassing the telemetry_status skip
    (used by the manual "update all sidecars" action)."""
    pending = media_items if force else _pending_items(media_items, state_file)
    written = 0
    failed = 0

    for item in pending:
        try:
            track_kind = fetch_item_telemetry(headers, item, output_dir)
        except Exception as exc:
            failed += 1
            LOGGER.warning("Telemetry fetch failed for %s: %s", item.filename, exc)
            if state_file:
                mark_telemetry(state_file, [item.key], STATUS_FAILED, str(exc))
            continue

        written += 1
        if state_file:
            mark_telemetry(state_file, [item.key], STATUS_COMPLETE)

        if progress:
            if track_kind:
                progress.emit_event(
                    "telemetry.track.completed",
                    f"{track_kind.upper()} processed for {item.filename}",
                    phase="telemetry",
                    job_id_guard=job_id,
                    set_message=False,
                    file_name=item.filename,
                    file_id=item.media_id,
                )
            progress.emit_event(
                "telemetry.mediainfo.completed",
                f"JSON processed for {item.filename}",
                phase="telemetry",
                job_id_guard=job_id,
                set_message=False,
                file_name=item.filename,
                file_id=item.media_id,
            )

    return written, failed
