from pathlib import Path


def extension_folder_name(filename: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    return extension or "no_extension"


def media_download_path(output_dir: Path, filename: str) -> Path:
    return output_dir / extension_folder_name(filename) / filename


def sidecar_output_path(output_dir: Path, filename: str, sidecar_filename: str) -> Path:
    return output_dir / extension_folder_name(filename) / sidecar_filename
