from pathlib import Path


def safe_child_path(base_dir: Path, candidate: Path) -> bool:
    try:
        resolved_base = base_dir.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_base)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def safe_filename(filename: str) -> str:
    return Path(filename).name


def extension_folder_name(filename: str) -> str:
    extension = Path(safe_filename(filename)).suffix.lower().lstrip(".")
    return extension or "no_extension"


def media_download_path(output_dir: Path, filename: str) -> Path:
    return output_dir / extension_folder_name(filename) / safe_filename(filename)


def sidecar_output_path(output_dir: Path, filename: str, sidecar_filename: str) -> Path:
    return (
        output_dir
        / extension_folder_name(filename)
        / safe_filename(sidecar_filename)
    )
