import json
from pathlib import Path

from gosync.downloader import (
    build_size_batches,
    format_media_for_log,
    format_size_mib,
    organize_extracted_media,
    parse_batch_max_bytes,
)
from gosync.manifest import MediaItem, format_extension_summary, read_manifest_from_har
from gosync.paths import extension_folder_name, media_download_path, sidecar_output_path
from gosync.runtime import prepare_manifest_state
from gosync.report import build_run_summary
from gosync.sidecar import run_sidecar_job
from gosync.state import load_state, sync_state_with_downloads


def media_item(media_id: str, filename: str, file_size: int | None) -> MediaItem:
    return MediaItem(
        key=f"{media_id}_{filename}",
        media_id=media_id,
        filename=filename,
        sidecar_filename=f"{filename}.xmp",
        file_size=file_size,
        metadata={
            "id": media_id,
            "filename": filename,
            "file_extension": Path(filename).suffix.lstrip("."),
            "file_size": file_size,
        },
    )


def write_har(path: Path) -> None:
    media = [
        {
            "id": "ABCDEFGHIJKLM",
            "filename": "GX010001.MP4",
            "file_extension": "MP4",
            "file_size": 100,
            "content_type": "video/mp4",
        },
        {
            "id": "NOPQRSTUVWXYZ",
            "filename": "GX010002.JPG",
            "file_extension": "JPG",
            "file_size": 50,
            "content_type": "image/jpeg",
        },
        {
            "id": "UNNAMEDMEDIA1",
            "file_extension": "MP4",
            "file_size": 25,
            "content_type": "video/mp4",
        },
        {
            "id": "ABCDEFGHIJKLM",
            "filename": "GX010001.MP4",
            "file_extension": "MP4",
            "file_size": 100,
            "content_type": "video/mp4",
        },
    ]
    path.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "url": "https://api.gopro.com/media/search?page=1"
                            },
                            "response": {
                                "content": {
                                    "text": json.dumps({"_embedded": {"media": media}})
                                }
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def test_manifest_deduplicates_by_id_filename(tmp_path: Path) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_har(har_path)

    manifest = read_manifest_from_har(har_path)

    assert [item.key for item in manifest.media] == [
        "ABCDEFGHIJKLM_GX010001.MP4",
        "NOPQRSTUVWXYZ_GX010002.JPG",
        "UNNAMEDMEDIA1_unnamed_1.MP4",
    ]
    assert len(manifest.duplicates) == 1
    assert manifest.duplicates[0].key == "ABCDEFGHIJKLM_GX010001.MP4"
    assert format_extension_summary(manifest) == "Media by extension: JPG: 1, MP4: 2"


def test_size_batches_use_largest_file_as_auto_cap() -> None:
    items = [
        media_item("A", "large.mp4", 100),
        media_item("B", "medium.mp4", 60),
        media_item("C", "small.jpg", 40),
        media_item("D", "tiny.jpg", 10),
    ]

    batch_cap = parse_batch_max_bytes("auto", items)
    batches = build_size_batches(items, batch_cap)

    assert batch_cap == 100
    assert [[item.filename for item in batch] for batch in batches] == [
        ["large.mp4"],
        ["medium.mp4", "small.jpg"],
        ["tiny.jpg"],
    ]


def test_media_log_format_includes_size() -> None:
    item = media_item("A", "large.mp4", 10 * 1024 * 1024)

    assert format_size_mib(item.file_size) == "10.00 MiB"
    assert format_media_for_log(item) == "large.mp4 (A, 10.00 MiB)"
    assert format_size_mib(None) == "unknown size"


def test_extension_directories_are_lowercase(tmp_path: Path) -> None:
    assert extension_folder_name("GX010001.MP4") == "mp4"
    assert extension_folder_name("GX010002.mp4") == "mp4"
    assert media_download_path(tmp_path, "GX010001.MP4") == (
        tmp_path / "mp4" / "GX010001.MP4"
    )
    assert sidecar_output_path(
        tmp_path,
        "GX010001.MP4",
        "GX010001.MP4.xmp",
    ) == tmp_path / "mp4" / "GX010001.MP4.xmp"


def test_organize_extracted_media_moves_files_into_extension_dirs(
    tmp_path: Path,
) -> None:
    item = media_item("A", "large.MP4", 10)
    output_dir = tmp_path / "downloads"
    output_dir.mkdir()
    (output_dir / "large.MP4").write_text("media", encoding="utf-8")

    organize_extracted_media(output_dir, [item])

    assert not (output_dir / "large.MP4").exists()
    assert (output_dir / "mp4" / "large.MP4").read_text(encoding="utf-8") == "media"


def test_state_sync_matches_download_folder(tmp_path: Path) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    manifest_file = tmp_path / "manifest.json"
    media_dump_file = tmp_path / "media_search.json"
    downloads = tmp_path / "downloads"

    manifest, state, changes = prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        manifest_file,
        media_dump_file,
    )

    assert len(manifest.media) == 3
    assert len(state["media"]) == 3
    assert changes == []
    dump = json.loads(media_dump_file.read_text(encoding="utf-8"))
    assert dump["matching_responses"] == 1
    assert dump["media_count"] == 4
    assert len(dump["media"]) == 4
    assert dump["media"][0]["filename"] == "GX010001.MP4"
    assert dump["media"][2]["filename"] == "unnamed_1.MP4"
    assert dump["media"][2]["id"] == "UNNAMEDMEDIA1"
    assert state["media"]["UNNAMEDMEDIA1_unnamed_1.MP4"]["id"] == "UNNAMEDMEDIA1"

    media_download_path(downloads, "GX010002.JPG").parent.mkdir(parents=True)
    media_download_path(downloads, "GX010002.JPG").write_text("done", encoding="utf-8")
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert {
        (change["filename"], change["status"]) for change in changes
    } == {("GX010002.JPG", "found")}
    jpg_record = state["media"]["NOPQRSTUVWXYZ_GX010002.JPG"]
    assert jpg_record["download_status"] == "downloaded"

    media_download_path(downloads, "GX010002.JPG").unlink()
    state, changes = sync_state_with_downloads(state_file, downloads)

    assert {
        (change["filename"], change["status"]) for change in changes
    } == {("GX010002.JPG", "missing")}
    assert state["media"]["NOPQRSTUVWXYZ_GX010002.JPG"]["download_status"] == "pending"


def test_sidecars_update_json_state(tmp_path: Path) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    manifest_file = tmp_path / "manifest.json"
    media_dump_file = tmp_path / "media_search.json"
    downloads = tmp_path / "downloads"
    manifest, _state, _changes = prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        manifest_file,
        media_dump_file,
    )

    run_sidecar_job(
        har_path,
        downloads,
        media_items=manifest.media,
        state_file=state_file,
    )
    state = load_state(state_file)

    assert sidecar_output_path(
        downloads,
        "GX010001.MP4",
        "GX010001.MP4.xmp",
    ).exists()
    assert {
        record["sidecar_status"] for record in state["media"].values()
    } == {"complete"}


def test_run_summary_includes_report_counts(tmp_path: Path) -> None:
    har_path = tmp_path / "gopro.com.har"
    write_har(har_path)
    state_file = tmp_path / "gosync_state.json"
    manifest_file = tmp_path / "manifest.json"
    media_dump_file = tmp_path / "media_search.json"
    downloads = tmp_path / "downloads"
    manifest, state, _changes = prepare_manifest_state(
        tmp_path,
        har_path,
        downloads,
        state_file,
        manifest_file,
        media_dump_file,
    )

    summary = build_run_summary(
        state,
        manifest,
        "complete",
        [{"id": "1", "filename": "a.mp4", "status": "found"}],
        tmp_path / "reports" / "run.json",
    )

    assert "Run summary" in summary
    assert "Total media: 3" in summary
    assert "Pending: 3" in summary
    assert "Skipped duplicates: 1" in summary
    assert "Resume sync changes: 1" in summary
    assert "Report:" in summary
