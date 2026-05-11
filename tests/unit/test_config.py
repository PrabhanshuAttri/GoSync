from pathlib import Path

import pytest

from gosync.config import resolve_inside_data_dir


def test_resolve_inside_data_dir_allows_relative_paths(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    assert resolve_inside_data_dir(data_dir, "downloads") == (
        data_dir / "downloads"
    ).resolve()


def test_resolve_inside_data_dir_allows_absolute_paths_inside_data_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "state.json"

    assert resolve_inside_data_dir(data_dir, str(target)) == target.resolve()


def test_resolve_inside_data_dir_rejects_parent_directory_traversal(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with pytest.raises(ValueError, match="inside data directory"):
        resolve_inside_data_dir(data_dir, "../outside")


def test_resolve_inside_data_dir_rejects_absolute_paths_outside_data_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with pytest.raises(ValueError, match="inside data directory"):
        resolve_inside_data_dir(data_dir, str(tmp_path / "outside"))


def test_resolve_inside_data_dir_rejects_symlinks_outside_data_dir(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    link = data_dir / "linked-outside"
    link.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="inside data directory"):
        resolve_inside_data_dir(data_dir, "linked-outside/file.txt")
