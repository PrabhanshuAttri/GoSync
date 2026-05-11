from pathlib import Path

from gosync.paths import (
    extension_folder_name,
    media_download_path,
    safe_child_path,
    sidecar_output_path,
)


def test_extension_folder_name_normalizes_case() -> None:
    assert extension_folder_name("GX010001.MP4") == "mp4"
    assert extension_folder_name("GX010002.mp4") == "mp4"
    assert extension_folder_name("GOPR0001.JpG") == "jpg"


def test_extension_folder_name_falls_back_for_extensionless_files() -> None:
    assert extension_folder_name("README") == "no_extension"


def test_media_and_sidecar_paths_share_extension_folder(tmp_path: Path) -> None:
    assert media_download_path(tmp_path, "GX010001.MP4") == (
        tmp_path / "mp4" / "GX010001.MP4"
    )
    assert sidecar_output_path(
        tmp_path,
        "GX010001.MP4",
        "GX010001.MP4.xmp",
    ) == tmp_path / "mp4" / "GX010001.MP4.xmp"


def test_media_and_sidecar_paths_strip_directory_components(tmp_path: Path) -> None:
    assert media_download_path(tmp_path, "../GX010001.MP4") == (
        tmp_path / "mp4" / "GX010001.MP4"
    )
    assert sidecar_output_path(
        tmp_path,
        "../GX010001.MP4",
        "../GX010001.MP4.xmp",
    ) == tmp_path / "mp4" / "GX010001.MP4.xmp"


def test_safe_child_path_rejects_paths_outside_base(tmp_path: Path) -> None:
    base_dir = tmp_path / "downloads"
    base_dir.mkdir()

    assert safe_child_path(base_dir, base_dir / "clip.mp4")
    assert not safe_child_path(base_dir, tmp_path / "clip.mp4")
