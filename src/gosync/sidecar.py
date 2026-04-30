import base64
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from gosync.progress import ProgressState


LOGGER = logging.getLogger("gosync.sidecar")

MEDIA_SEARCH_URL = "https://api.gopro.com/media/search"
MEDIA_LIST_KEYS = ("media", "items", "data")
STANDARD_FIELDS = {
    "camera_model",
    "captured_at",
    "captured_at_timezone",
    "content_title",
    "content_type",
    "created_at",
    "file_extension",
    "file_size",
    "filename",
    "firmware_version",
    "fov",
    "gopro_user_id",
    "height",
    "id",
    "mce_type",
    "orientation",
    "play_as",
    "resolution",
    "source_duration",
    "source_gumi",
    "source_mgumi",
    "stabilized",
    "submitted_at",
    "token",
    "type",
    "updated_at",
    "width",
}
COMMON_SIDECAR_FIELDS = {
    "ai_training_opt_out",
    "camera_model",
    "captured_at",
    "captured_at_timezone",
    "content_title",
    "content_type",
    "created_at",
    "file_extension",
    "file_size",
    "filename",
    "firmware_version",
    "fov",
    "height",
    "orientation",
    "play_as",
    "ready_to_view",
    "resolution",
    "submitted_at",
    "thumbnail_available",
    "type",
    "updated_at",
    "width",
}
VIDEO_SIDECAR_FIELDS = COMMON_SIDECAR_FIELDS | {
    "available_labels",
    "mce_type",
    "moments_count",
    "ready_to_edit",
    "source_duration",
    "stabilized",
}
IMAGE_SIDECAR_FIELDS = COMMON_SIDECAR_FIELDS


def xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def xml_escape(value: Any) -> str:
    return escape(xml_value(value), {'"': "&quot;"})


def normalize_datetime(value: Any) -> str:
    if not value:
        return ""

    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return str(value)

    return parsed.isoformat()


def xmp_property_name(key: str) -> str:
    parts = re.split(r"[^0-9A-Za-z]+", key)
    name = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not name:
        return "Field"
    if name[0].isdigit():
        return f"Field{name}"
    return name


def is_media_file(item: Any) -> bool:
    if not isinstance(item, dict):
        return False

    filename = item.get("filename")
    extension = item.get("file_extension") or Path(str(filename or "")).suffix.lstrip(".")
    if not filename or not extension:
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
        print(
            f"Warning: skipped unparsable media/search response #{entry_number}",
            file=sys.stderr,
        )
        return None


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


def sidecar_stem(metadata: dict[str, Any]) -> str:
    filename = Path(str(metadata["filename"])).name
    extension = str(metadata.get("file_extension") or Path(filename).suffix).lstrip(".")

    if not extension:
        return filename

    suffix = f".{extension.lower()}"
    if filename.lower().endswith(suffix):
        return filename

    return f"{filename}.{extension}"


def media_kind(metadata: dict[str, Any]) -> str:
    content_type = str(metadata.get("content_type", "")).lower()
    item_type = str(metadata.get("type") or metadata.get("play_as") or "").lower()
    extension = str(
        metadata.get("file_extension") or Path(str(metadata.get("filename", ""))).suffix
    ).lower()

    if content_type.startswith("video/") or item_type == "video":
        return "video"
    if content_type.startswith("image/") or item_type in {"image", "photo"}:
        return "image"
    if extension.lstrip(".") in {"mp4", "mov", "lrv", "360"}:
        return "video"
    if extension.lstrip(".") in {"jpg", "jpeg", "png", "gpr", "dng", "heic"}:
        return "image"
    return "media"


def sidecar_field_names(metadata: dict[str, Any]) -> set[str]:
    kind = media_kind(metadata)
    if kind == "video":
        return VIDEO_SIDECAR_FIELDS
    if kind == "image":
        return IMAGE_SIDECAR_FIELDS
    return COMMON_SIDECAR_FIELDS


def extract_media_items(response_json: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for candidate_list in iter_candidate_lists(response_json):
        for item in candidate_list:
            if is_media_file(item):
                items.append(item)

    if not items:
        collect_media_items(response_json, items)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        identity = str(item.get("id") or sidecar_stem(item))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(item)

    return deduped


def read_media_from_har(har_path: Path) -> tuple[list[dict[str, Any]], int]:
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

    media_items: list[dict[str, Any]] = []
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

        media_items.extend(extract_media_items(response_json))

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in media_items:
        identity = str(item.get("id") or sidecar_stem(item))
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(item)

    return deduped, matching_entries


def build_xmp(metadata: dict[str, Any]) -> str:
    captured_at = normalize_datetime(metadata.get("captured_at"))
    created_at = normalize_datetime(metadata.get("created_at"))
    updated_at = normalize_datetime(metadata.get("updated_at"))
    submitted_at = normalize_datetime(metadata.get("submitted_at"))
    title = metadata.get("content_title") or metadata.get("filename")

    standard_attributes: list[tuple[str, Any]] = [
        ("xmp:CreateDate", captured_at or created_at or submitted_at),
        ("xmp:ModifyDate", updated_at),
        ("xmp:MetadataDate", updated_at or created_at or submitted_at),
        ("dc:format", metadata.get("content_type")),
        ("tiff:Model", metadata.get("camera_model")),
        ("exif:PixelXDimension", metadata.get("width")),
        ("exif:PixelYDimension", metadata.get("height")),
    ]
    standard_xml = "\n".join(
        f'   {name}="{xml_escape(value)}"'
        for name, value in standard_attributes
        if value not in (None, "")
    )

    allowed_fields = sidecar_field_names(metadata)
    gopro_fields = {
        key: metadata[key]
        for key in sorted(allowed_fields.intersection(metadata.keys()))
        if metadata[key] not in (None, "")
    }
    gopro_xml = "\n".join(
        f'   gopro:{xmp_property_name(key)}="{xml_escape(value)}"'
        for key, value in gopro_fields.items()
    )
    attribute_xml = "\n".join(part for part in (standard_xml, gopro_xml) if part)

    title_xml = ""
    if title:
        title_xml = (
            "\n   <dc:title>\n"
            "    <rdf:Alt>\n"
            f"     <rdf:li xml:lang=\"x-default\">{xml_escape(title)}</rdf:li>\n"
            "    </rdf:Alt>\n"
            "   </dc:title>"
        )

    return f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
   xmlns:exif="http://ns.adobe.com/exif/1.0/"
   xmlns:gopro="https://gopro.com/ns/media/1.0/"
{attribute_xml}>{title_xml}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_sidecars(media_items: list[dict[str, Any]], output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for metadata in media_items:
        sidecar_path = output_dir / f"{sidecar_stem(metadata)}.xmp"
        sidecar_path.write_text(build_xmp(metadata), encoding="utf-8")
        written += 1

    return written


def generate_sidecars_from_har(har_path: Path, output_dir: Path) -> tuple[int, int]:
    media_items, matching_entries = read_media_from_har(har_path)
    if matching_entries == 0:
        raise ValueError(f"No API calls to {MEDIA_SEARCH_URL} found in HAR file")
    if not media_items:
        raise ValueError("No media file metadata found in media/search responses")

    written = write_sidecars(media_items, output_dir)
    return written, matching_entries


def run_sidecar_job(
    har_path: Path,
    output_dir: Path,
    progress: ProgressState | None = None,
) -> None:
    try:
        if progress:
            message = f"Generating XMP sidecars in {output_dir}..."
            progress.update(
                sidecar_status="running",
                sidecar_dir=str(output_dir),
                sidecar_message=message,
            )
            LOGGER.info(message)
            progress.notify("info", "XMP sidecars", "Generating XMP sidecar files.")

        written, matching_entries = generate_sidecars_from_har(
            har_path,
            output_dir,
        )

        if progress:
            file_label = "file" if written == 1 else "files"
            response_label = "response" if matching_entries == 1 else "responses"
            message = (
                f"Generated {written} XMP sidecar {file_label} from "
                f"{matching_entries} media/search {response_label}."
            )
            progress.update(
                sidecar_status="complete",
                sidecar_count=written,
                sidecar_dir=str(output_dir),
                sidecar_message=message,
            )
            LOGGER.info(message)
            progress.notify(
                "success",
                "XMP sidecars complete",
                f"Generated {written} XMP sidecar {file_label}.",
            )
    except Exception as exc:
        if progress:
            message = f"XMP sidecar generation failed: {exc}"
            progress.update(
                sidecar_status="failed",
                sidecar_dir=str(output_dir),
                sidecar_message=message,
            )
            LOGGER.exception("XMP sidecar generation failed.")
            progress.notify("error", "XMP sidecars failed", str(exc))
        else:
            raise
