import json
from pathlib import Path

from gosync.verify import (
    discover_chaptered_filenames,
    find_chapter_paths,
    packet_counts,
    payload_mismatch_detail,
    stream_signature,
)


def test_packet_counts_sums_by_codec_type_and_tag() -> None:
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_tag_string": "avc1",
                "nb_read_packets": "100",
            },
            {
                "codec_type": "audio",
                "codec_tag_string": "mp4a",
                "nb_read_packets": "50",
            },
            {
                "codec_type": "data",
                "codec_tag_string": "gpmd",
                "nb_read_packets": "20",
            },
            # A second video stream with the same (type, tag) accumulates.
            {
                "codec_type": "video",
                "codec_tag_string": "avc1",
                "nb_read_packets": "10",
            },
        ]
    }

    counts = packet_counts(probe)

    assert counts == {
        ("video", "avc1"): 110,
        ("audio", "mp4a"): 50,
        ("data", "gpmd"): 20,
    }


def test_packet_counts_handles_missing_packet_count() -> None:
    probe = {"streams": [{"codec_type": "data", "codec_tag_string": "tmcd"}]}

    assert packet_counts(probe) == {("data", "tmcd"): 0}


def test_stream_signature_extracts_type_tag_and_codec_name() -> None:
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_tag_string": "avc1",
                "codec_name": "h264",
            },
            {
                "codec_type": "data",
                "codec_tag_string": "gpmd",
                "codec_name": "bin_data",
            },
        ]
    }

    assert stream_signature(probe) == [
        ("video", "avc1", "h264"),
        ("data", "gpmd", "bin_data"),
    ]


def test_payload_mismatch_detail_reports_size_delta_and_first_diff_offset() -> None:
    detail = payload_mismatch_detail(b"abcdef", b"abcXef")

    assert "6 bytes" in detail
    assert "delta=+0" in detail
    assert "offset 3" in detail


def test_payload_mismatch_detail_reports_length_delta() -> None:
    detail = payload_mismatch_detail(b"abcdef", b"abc")

    assert "delta=-3" in detail


def test_discover_chaptered_filenames_filters_to_item_count_above_one(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "media": [
                    {"filename": "GX010320.MP4", "metadata": {"item_count": 3}},
                    {"filename": "clip.mp4", "metadata": {"item_count": 1}},
                    {"filename": "photo.jpg", "metadata": {}},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = discover_chaptered_filenames(manifest_path)

    assert result == [("GX010320.MP4", 3)]


def test_discover_chaptered_filenames_raises_when_manifest_missing(
    tmp_path: Path,
) -> None:
    try:
        discover_chaptered_filenames(tmp_path / "missing.json")
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass


def test_find_chapter_paths_uses_production_chapter_finder(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    for name in ("GX010320.MP4", "GX020320.MP4"):
        (output_dir / name).write_text(name, encoding="utf-8")

    result = find_chapter_paths(output_dir, "GX010320.MP4")

    assert result == [
        output_dir / "GX010320.MP4",
        output_dir / "GX020320.MP4",
    ]


def test_find_chapter_paths_returns_empty_list_when_fewer_than_two(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "GX010320.MP4").write_text("chapter-1", encoding="utf-8")

    assert find_chapter_paths(output_dir, "GX010320.MP4") == []
