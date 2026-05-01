from pathlib import Path

from gosync.paths import extension_folder_name, media_download_path, sidecar_output_path


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

