import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from gosync.constants import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from gosync.manifest import (
    MediaItem,
    read_manifest_from_har,
)
from gosync.paths import sidecar_output_path
from gosync.progress import ProgressState
from gosync.state import mark_sidecars
from gosync.telemetry import media_stem

LOGGER = logging.getLogger("gosync.sidecar")


def xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def xml_escape(value: Any) -> str:
    return escape(xml_value(value), {'"': "&quot;"})


def sidecar_stem(metadata: dict[str, Any]) -> str:
    filename = Path(str(metadata["filename"])).name
    extension = str(metadata.get("file_extension") or Path(filename).suffix).lstrip(".")

    if not extension:
        return filename

    suffix = f".{extension.lower()}"
    if filename.lower().endswith(suffix):
        return filename

    return f"{filename}.{extension}"


def format_captured_datetime(metadata: dict[str, Any]) -> str:
    """Immich/EXIF-style local capture timestamp: captured_at (a UTC instant)
    converted into the camera's local offset from captured_at_timezone, e.g.
    2026-07-11T13:32:32.000-10:00 -- not just captured_at relabeled."""
    raw = metadata.get("captured_at") or metadata.get("created_at") or metadata.get(
        "submitted_at"
    )
    if not raw:
        return ""

    text = str(raw)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return str(raw)

    tz_offset = str(metadata.get("captured_at_timezone") or "").strip()
    if tz_offset:
        try:
            sign = -1 if tz_offset.startswith("-") else 1
            hours, minutes = tz_offset.lstrip("+-").split(":")
            offset = sign * timedelta(hours=int(hours), minutes=int(minutes))
            parsed = parsed.astimezone(timezone(offset))
        except (ValueError, AttributeError):
            pass

    offset = parsed.utcoffset() or timedelta(0)
    offset_minutes = int(offset.total_seconds() // 60)
    offset_sign = "+" if offset_minutes >= 0 else "-"
    offset_minutes = abs(offset_minutes)
    offset_text = f"{offset_sign}{offset_minutes // 60:02d}:{offset_minutes % 60:02d}"
    return (
        f"{parsed.strftime('%Y-%m-%dT%H:%M:%S')}"
        f".{parsed.microsecond // 1000:03d}{offset_text}"
    )


def gps_dms(value: float, positive: str, negative: str) -> str:
    """EXIF-style degrees,decimal-minutes-with-hemisphere, e.g. 37,20.250N."""
    hemisphere = positive if value >= 0 else negative
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60
    return f"{degrees},{minutes:.3f}{hemisphere}"


def gps_altitude(value: float) -> tuple[str, str]:
    """EXIF GPSAltitude as a rational string plus its GPSAltitudeRef
    (0 = above sea level, 1 = below)."""
    ref = "0" if value >= 0 else "1"
    return f"{round(abs(value) * 1000)}/1000", ref


def geo_from_telemetry(output_dir: Path, filename: str) -> dict[str, float] | None:
    """Reuse GPS coordinates already fetched by the telemetry step (step 6),
    if a mediainfo.json for this item exists from a previous run."""
    json_path = output_dir / "json" / f"{media_stem(filename)}_mediainfo.json"
    if not json_path.exists():
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    geo = payload.get("media", {}).get("geoData")
    if isinstance(geo, dict) and geo.get("latitude") is not None and geo.get(
        "longitude"
    ) is not None:
        return geo
    return None


def build_xmp(metadata: dict[str, Any], geo: dict[str, float] | None = None) -> str:
    captured = format_captured_datetime(metadata)
    description = metadata.get("content_description") or metadata.get("content_title")
    tags = [
        tag.strip()
        for tag in str(metadata.get("tags") or "").split(",")
        if tag.strip()
    ]
    camera_model = metadata.get("camera_model")
    if camera_model:
        tags.append(f"GoPro {camera_model}")

    elements: list[str] = []
    if description:
        escaped_description = xml_escape(description)
        elements.append(f"   <dc:description>{escaped_description}</dc:description>")
    if tags:
        tag_items = "\n".join(
            f"     <rdf:li>{xml_escape(tag)}</rdf:li>" for tag in tags
        )
        elements.append(
            "   <digiKam:TagsList>\n    <rdf:Seq>\n"
            f"{tag_items}\n    </rdf:Seq>\n   </digiKam:TagsList>"
        )
    if captured:
        elements.append(f"   <exif:DateTimeOriginal>{captured}</exif:DateTimeOriginal>")
        elements.append(f"   <xmp:CreateDate>{captured}</xmp:CreateDate>")
        elements.append(f"   <photoshop:DateCreated>{captured}</photoshop:DateCreated>")
    if geo:
        latitude = gps_dms(geo["latitude"], "N", "S")
        longitude = gps_dms(geo["longitude"], "E", "W")
        elements.append(f"   <exif:GPSLatitude>{latitude}</exif:GPSLatitude>")
        elements.append(f"   <exif:GPSLongitude>{longitude}</exif:GPSLongitude>")
        if geo.get("altitude") is not None:
            fraction, ref = gps_altitude(geo["altitude"])
            elements.append(f"   <exif:GPSAltitude>{fraction}</exif:GPSAltitude>")
            elements.append(f"   <exif:GPSAltitudeRef>{ref}</exif:GPSAltitudeRef>")
    if camera_model:
        elements.append("   <tiff:Make>GoPro</tiff:Make>")
        elements.append(f"   <tiff:Model>{xml_escape(camera_model)}</tiff:Model>")

    body = "\n".join(elements)

    return f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 6.0.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:digiKam="http://www.digikam.org/ns/1.0/">
{body}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_sidecars_for_manifest(
    media_items: list[MediaItem],
    output_dir: Path,
    state_file: Path | None = None,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    written_keys: list[str] = []

    for media_item in media_items:
        # Skip JSON files
        if media_item.filename.lower().endswith('.json'):
            continue
            
        sidecar_path = sidecar_output_path(
            output_dir,
            media_item.filename,
            media_item.sidecar_filename,
        )
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        geo = geo_from_telemetry(output_dir, media_item.filename)
        sidecar_path.write_text(build_xmp(media_item.metadata, geo), encoding="utf-8")
        written_keys.append(media_item.key)

    if state_file:
        mark_sidecars(state_file, written_keys, STATUS_COMPLETE)

    return len(written_keys)


def generate_sidecars_from_har(har_path: Path, output_dir: Path) -> tuple[int, int]:
    manifest = read_manifest_from_har(har_path)
    written = write_sidecars_for_manifest(manifest.media, output_dir)
    return written, manifest.matching_entries


def run_sidecar_job(
    har_path: Path,
    output_dir: Path,
    progress: ProgressState | None = None,
    media_items: list[MediaItem] | None = None,
    state_file: Path | None = None,
    job_id: str | None = None,
) -> None:
    try:
        if progress:
            message = f"Generating XMP sidecars in {output_dir}..."
            progress.update(
                job_id_guard=job_id,
                sidecar_status=STATUS_RUNNING,
                sidecar_dir=str(output_dir),
                sidecar_message=message,
            )
            LOGGER.info(message)
            progress.emit_event(
                "sidecar.generation.started",
                "Generating XMP sidecar files",
                level="ACTIVE",
                phase="sidecars",
                job_id_guard=job_id,
                set_message=False,
                destination=str(output_dir),
            )
            progress.notify(
                "info",
                "XMP sidecars",
                "Generating XMP sidecar files.",
                job_id_guard=job_id,
            )

        if media_items is None:
            manifest = read_manifest_from_har(har_path)
            media_items = manifest.media
            matching_entries = manifest.matching_entries
        else:
            matching_entries = 0

        written = write_sidecars_for_manifest(media_items, output_dir, state_file)

        if progress:
            file_label = "file" if written == 1 else "files"
            response_label = "response" if matching_entries == 1 else "responses"
            if matching_entries:
                message = (
                    f"Generated {written} XMP sidecar {file_label} from "
                    f"{matching_entries} media/search {response_label}."
                )
            else:
                message = f"Generated {written} XMP sidecar {file_label}."
            progress.update(
                job_id_guard=job_id,
                sidecar_status=STATUS_COMPLETE,
                sidecar_count=written,
                sidecar_dir=str(output_dir),
                sidecar_message=message,
            )
            LOGGER.info(message)
            progress.emit_event(
                "sidecar.generation.completed",
                message,
                level="SUCCESS",
                phase="sidecars",
                job_id_guard=job_id,
                set_message=False,
                sidecar_count=written,
                written_count=written,
                destination=str(output_dir),
                matching_entries=matching_entries,
            )
            progress.notify(
                "success",
                "XMP sidecars complete",
                f"Generated {written} XMP sidecar {file_label}.",
                job_id_guard=job_id,
            )
    except Exception as exc:
        if state_file and media_items:
            mark_sidecars(
                state_file,
                [item.key for item in media_items],
                STATUS_FAILED,
                str(exc),
            )
        if progress:
            message = f"XMP sidecar generation failed: {exc}"
            progress.update(
                job_id_guard=job_id,
                sidecar_status=STATUS_FAILED,
                sidecar_dir=str(output_dir),
                sidecar_message=message,
            )
            LOGGER.exception("XMP sidecar generation failed.")
            progress.emit_event(
                "sidecar.generation.failed",
                message,
                level="ERROR",
                phase="sidecars",
                job_id_guard=job_id,
                set_message=False,
                error_type=type(exc).__name__,
                error_message=str(exc),
                destination=str(output_dir),
            )
            progress.notify(
                "error",
                "XMP sidecars failed",
                str(exc),
                job_id_guard=job_id,
            )
        else:
            raise
