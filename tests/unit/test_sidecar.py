from pathlib import Path

from gosync.paths import sidecar_output_path
from gosync.sidecar import (
    build_xmp,
    generate_sidecars_from_har,
    media_kind,
    normalize_datetime,
    sidecar_field_names,
    sidecar_stem,
    write_sidecars_for_manifest,
    xml_escape,
    xmp_property_name,
)


def test_xml_helpers_escape_values_and_normalize_property_names() -> None:
    assert xml_escape('A&B "quote"') == "A&amp;B &quot;quote&quot;"
    assert xml_escape({"b": 2, "a": 1}) == "{&quot;a&quot;:1,&quot;b&quot;:2}"
    assert xmp_property_name("captured_at_timezone") == "CapturedAtTimezone"
    assert xmp_property_name("360_mode") == "Field360Mode"
    assert xmp_property_name("---") == "Field"


def test_normalize_datetime_converts_z_suffix() -> None:
    assert normalize_datetime("2026-04-01T12:30:00Z") == "2026-04-01T12:30:00+00:00"
    assert normalize_datetime("not-a-date") == "not-a-date"
    assert normalize_datetime(None) == ""


def test_sidecar_stem_does_not_duplicate_extension() -> None:
    assert sidecar_stem({"filename": "GX010001.MP4", "file_extension": "MP4"}) == (
        "GX010001.MP4"
    )
    assert sidecar_stem({"filename": "GX010001", "file_extension": "MP4"}) == (
        "GX010001.MP4"
    )


def test_media_kind_uses_content_type_type_and_extension() -> None:
    assert media_kind({"content_type": "video/mp4"}) == "video"
    assert media_kind({"type": "photo"}) == "image"
    assert media_kind({"filename": "clip.360"}) == "video"
    assert media_kind({"filename": "raw.gpr"}) == "image"
    assert media_kind({"filename": "metadata.bin"}) == "media"


def test_sidecar_fields_include_video_specific_fields_only_for_video() -> None:
    assert "source_duration" in sidecar_field_names({"content_type": "video/mp4"})
    assert "source_duration" not in sidecar_field_names({"content_type": "image/jpeg"})


def test_build_xmp_includes_safe_selected_fields_and_escapes_title() -> None:
    xmp = build_xmp(
        {
            "filename": "GX010001.MP4",
            "file_extension": "MP4",
            "content_type": "video/mp4",
            "content_title": 'Surf & "Sun"',
            "captured_at": "2026-04-01T12:30:00Z",
            "source_duration": 12.5,
            "private_token": "must not leak",
        }
    )

    assert "xmp:CreateDate=\"2026-04-01T12:30:00+00:00\"" in xmp
    assert "dc:format=\"video/mp4\"" in xmp
    assert "Surf &amp; &quot;Sun&quot;" in xmp
    assert "gopro:SourceDuration=\"12.5\"" in xmp
    assert "private_token" not in xmp


def test_write_sidecars_for_manifest_places_files_next_to_media(
    tmp_path: Path,
    make_media_item,
) -> None:
    item = make_media_item("A", "GX010001.MP4", 100)

    written = write_sidecars_for_manifest([item], tmp_path / "downloads")

    assert written == 1
    assert sidecar_output_path(
        tmp_path / "downloads",
        item.filename,
        item.sidecar_filename,
    ).is_file()


def test_generate_sidecars_from_har_returns_written_count_and_matching_entries(
    tmp_path: Path,
    write_sample_har,
) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_sample_har(har_path)

    written, matching_entries = generate_sidecars_from_har(
        har_path,
        tmp_path / "downloads",
    )

    assert written == 3
    assert matching_entries == 1

