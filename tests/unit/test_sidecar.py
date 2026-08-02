import json
from pathlib import Path

from gosync.paths import sidecar_output_path
from gosync.sidecar import (
    build_xmp,
    format_captured_datetime,
    generate_sidecars_from_har,
    geo_from_telemetry,
    gps_altitude,
    gps_dms,
    sidecar_stem,
    technical_info_from_telemetry,
    write_sidecars_for_manifest,
    xml_escape,
)


def test_xml_escape_escapes_ampersand_and_quotes() -> None:
    assert xml_escape('A&B "quote"') == "A&amp;B &quot;quote&quot;"
    assert xml_escape({"b": 2, "a": 1}) == "{&quot;a&quot;:1,&quot;b&quot;:2}"


def test_sidecar_stem_does_not_duplicate_extension() -> None:
    assert sidecar_stem({"filename": "GX010001.MP4", "file_extension": "MP4"}) == (
        "GX010001.MP4"
    )
    assert sidecar_stem({"filename": "GX010001", "file_extension": "MP4"}) == (
        "GX010001.MP4"
    )


def test_format_captured_datetime_converts_to_local_offset() -> None:
    result = format_captured_datetime(
        {"captured_at": "2026-07-11T23:32:32Z", "captured_at_timezone": "-10:00"}
    )
    assert result == "2026-07-11T13:32:32.000-10:00"


def test_format_captured_datetime_defaults_to_utc_without_timezone() -> None:
    result = format_captured_datetime({"captured_at": "2026-04-01T12:30:00Z"})
    assert result == "2026-04-01T12:30:00.000+00:00"


def test_format_captured_datetime_falls_back_through_dates() -> None:
    assert format_captured_datetime({"created_at": "2026-04-01T12:30:00Z"}) == (
        "2026-04-01T12:30:00.000+00:00"
    )
    assert format_captured_datetime({}) == ""


def test_gps_dms_formats_degrees_minutes_and_hemisphere() -> None:
    assert gps_dms(37.3375, "N", "S") == "37,20.250N"
    assert gps_dms(-121.884, "E", "W") == "121,53.040W"


def test_gps_altitude_returns_fraction_and_ref() -> None:
    assert gps_altitude(25.0) == ("25000/1000", "0")
    assert gps_altitude(-16.819) == ("16819/1000", "1")


def test_geo_from_telemetry_reads_existing_mediainfo_json(tmp_path: Path) -> None:
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    (json_dir / "GX010001_mediainfo.json").write_text(
        json.dumps({"media": {"geoData": {"latitude": 1.0, "longitude": 2.0}}}),
        encoding="utf-8",
    )

    geo = geo_from_telemetry(tmp_path, "GX010001.MP4")

    assert geo == {"latitude": 1.0, "longitude": 2.0}


def test_geo_from_telemetry_returns_none_when_missing(tmp_path: Path) -> None:
    assert geo_from_telemetry(tmp_path, "GX010001.MP4") is None


def test_build_xmp_includes_dates_tags_description_and_camera_model() -> None:
    xmp = build_xmp(
        {
            "filename": "GX010001.MP4",
            "content_title": 'Surf & "Sun"',
            "captured_at": "2026-04-01T12:30:00Z",
            "camera_model": "HERO11 Black",
            "tags": "Vacation, Family",
            "private_token": "must not leak",
        }
    )

    assert "<xmp:CreateDate>2026-04-01T12:30:00.000+00:00</xmp:CreateDate>" in xmp
    assert (
        "<exif:DateTimeOriginal>2026-04-01T12:30:00.000+00:00</exif:DateTimeOriginal>"
        in xmp
    )
    assert (
        "<photoshop:DateCreated>2026-04-01T12:30:00.000+00:00</photoshop:DateCreated>"
        in xmp
    )
    assert "<dc:description>Surf &amp; &quot;Sun&quot;</dc:description>" in xmp
    assert "<rdf:li>Vacation</rdf:li>" in xmp
    assert "<rdf:li>Family</rdf:li>" in xmp
    assert "<tiff:Make>GoPro</tiff:Make>" in xmp
    assert "<tiff:Model>HERO11 Black</tiff:Model>" in xmp
    assert "<rdf:li>GoPro HERO11 Black</rdf:li>" in xmp
    assert "private_token" not in xmp
    assert "must not leak" not in xmp


def test_build_xmp_adds_camera_model_tag_even_without_other_tags() -> None:
    xmp = build_xmp({"filename": "GX010001.MP4", "camera_model": "HERO11 Black"})

    assert "digiKam:TagsList" in xmp
    assert "<rdf:li>GoPro HERO11 Black</rdf:li>" in xmp


def test_build_xmp_omits_missing_fields() -> None:
    xmp = build_xmp({"filename": "GX010001.MP4"})

    assert "dc:description" not in xmp
    assert "digiKam:TagsList" not in xmp
    assert "exif:GPSLatitude" not in xmp
    assert "tiff:Make" not in xmp


def test_build_xmp_includes_gps_when_geo_provided() -> None:
    xmp = build_xmp(
        {"filename": "GX010001.MP4"},
        geo={"latitude": 37.3375, "longitude": -121.884, "altitude": -16.819},
    )

    assert "<exif:GPSLatitude>37,20.250N</exif:GPSLatitude>" in xmp
    assert "<exif:GPSLongitude>121,53.040W</exif:GPSLongitude>" in xmp
    assert "<exif:GPSAltitude>16819/1000</exif:GPSAltitude>" in xmp
    assert "<exif:GPSAltitudeRef>1</exif:GPSAltitudeRef>" in xmp


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


def test_write_sidecars_for_manifest_picks_up_existing_telemetry_geo(
    tmp_path: Path,
    make_media_item,
) -> None:
    output_dir = tmp_path / "downloads"
    json_dir = output_dir / "json"
    json_dir.mkdir(parents=True)
    (json_dir / "GX010001_mediainfo.json").write_text(
        json.dumps({"media": {"geoData": {"latitude": 1.5, "longitude": 2.5}}}),
        encoding="utf-8",
    )
    item = make_media_item("A", "GX010001.MP4", 100)

    write_sidecars_for_manifest([item], output_dir)

    sidecar_path = sidecar_output_path(output_dir, item.filename, item.sidecar_filename)
    assert "exif:GPSLatitude" in sidecar_path.read_text(encoding="utf-8")


def test_build_xmp_writes_dimensions_orientation_and_software_from_flat_metadata() -> (
    None
):
    xmp = build_xmp(
        {
            "filename": "GX010001.MP4",
            "width": 3840,
            "height": 2160,
            "orientation": 1,
            "firmware_version": "H21.01.01.42.00",
        }
    )

    assert "<tiff:ImageWidth>3840</tiff:ImageWidth>" in xmp
    assert "<tiff:ImageLength>2160</tiff:ImageLength>" in xmp
    assert "<tiff:Orientation>1</tiff:Orientation>" in xmp
    assert "<tiff:Software>H21.01.01.42.00</tiff:Software>" in xmp


def test_build_xmp_prefers_technical_info_over_flat_metadata() -> None:
    xmp = build_xmp(
        {"filename": "GX010001.MP4", "width": 3840, "height": 2160, "orientation": 1},
        technical={"width": 5312, "height": 2988, "orientation": 3},
    )

    assert "<tiff:ImageWidth>5312</tiff:ImageWidth>" in xmp
    assert "<tiff:ImageLength>2988</tiff:ImageLength>" in xmp
    assert "<tiff:Orientation>3</tiff:Orientation>" in xmp


def test_build_xmp_writes_duration_only_from_technical_info() -> None:
    without_technical = build_xmp(
        {"filename": "GX010001.MP4", "source_duration": "9908732"}
    )
    assert "xmpDM:duration" not in without_technical

    with_technical = build_xmp(
        {"filename": "GX010001.MP4"},
        technical={"duration_seconds": 1537.536},
    )
    assert '<xmpDM:duration rdf:parseType="Resource">' in with_technical
    assert "<xmpDM:value>1537.536</xmpDM:value>" in with_technical
    assert "<xmpDM:scale>1/1</xmpDM:scale>" in with_technical


def test_build_xmp_adds_searchable_tags_for_fov_stabilization_camera_and_type() -> (
    None
):
    xmp = build_xmp(
        {
            "filename": "GX010001.MP4",
            "location_name": "Yosemite",
            "type": "TimeLapseVideo",
        },
        technical={"fov": "linear", "lens": "front", "eis_active": "HS EIS"},
    )

    assert "<rdf:li>FOV: linear</rdf:li>" in xmp
    assert "<rdf:li>Stabilized: HS EIS</rdf:li>" in xmp
    assert "<rdf:li>Camera: front</rdf:li>" in xmp
    assert "<rdf:li>Location: Yosemite</rdf:li>" in xmp
    assert "<rdf:li>Type: TimeLapseVideo</rdf:li>" in xmp


def test_build_xmp_omits_default_camera_position() -> None:
    xmp = build_xmp(
        {"filename": "GX010001.MP4"},
        technical={"lens": "default"},
    )

    assert "Camera:" not in xmp


def test_technical_info_from_telemetry_reads_mediainfo_task_result(
    tmp_path: Path,
) -> None:
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    (json_dir / "GX010001_mediainfo.json").write_text(
        json.dumps(
            {
                "media": {},
                "mediainfo": {
                    "task_result": {
                        "duration": 1537.536,
                        "encoded_width": 5312,
                        "encoded_height": 2988,
                        "exif_orientation": 1,
                        "software": "H22.01.02.32.00",
                        "gopro": {
                            "fov": "linear",
                            "lens": "front",
                            "eis": True,
                            "eis_active": "HS EIS",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    info = technical_info_from_telemetry(tmp_path, "GX010001.MP4")

    assert info == {
        "duration_seconds": 1537.536,
        "width": 5312,
        "height": 2988,
        "orientation": 1,
        "software": "H22.01.02.32.00",
        "fov": "linear",
        "lens": "front",
        "eis_active": "HS EIS",
    }


def test_technical_info_from_telemetry_returns_empty_when_missing(
    tmp_path: Path,
) -> None:
    assert technical_info_from_telemetry(tmp_path, "GX010001.MP4") == {}


def test_write_sidecars_for_manifest_picks_up_existing_technical_info(
    tmp_path: Path,
    make_media_item,
) -> None:
    output_dir = tmp_path / "downloads"
    json_dir = output_dir / "json"
    json_dir.mkdir(parents=True)
    (json_dir / "GX010001_mediainfo.json").write_text(
        json.dumps(
            {
                "media": {},
                "mediainfo": {"task_result": {"duration": 12.5, "encoded_width": 100}},
            }
        ),
        encoding="utf-8",
    )
    item = make_media_item("A", "GX010001.MP4", 100)

    write_sidecars_for_manifest([item], output_dir)

    sidecar_path = sidecar_output_path(output_dir, item.filename, item.sidecar_filename)
    xmp = sidecar_path.read_text(encoding="utf-8")
    assert "xmpDM:duration" in xmp
    assert "<tiff:ImageWidth>100</tiff:ImageWidth>" in xmp


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
