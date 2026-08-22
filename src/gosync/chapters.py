import re
import shutil
import subprocess
from pathlib import Path

from gosync.config import (
    FFPROBE_BINARY,
    FFPROBE_TIMEOUT_SECONDS,
    MAX_TOLERANCE_BYTES,
    MIN_TOLERANCE_BYTES,
    PER_CHAPTER_OVERHEAD_BYTES,
)
from gosync.constants import ORIGINAL_UNMERGED_FOLDER_PREFIX
from gosync.manifest import MediaItem
from gosync.paths import safe_child_path

# GoPro chapter filenames encode play order in the filename itself:
#   - modern: G[HX]<chapter 2-digit><group 4-digit>.<ext>, e.g. GX010320.MP4
#     (chapter 01) and GX020320.MP4 (chapter 02) of group 0320.
#   - legacy: GOPR<group 4-digit>.<ext> is chapter 1, followed by
#     G[PH]<chapter 2-digit><group 4-digit>.<ext> for later chapters.
# Embedded container metadata cannot be used for ordering: GoPro stamps every
# chapter of a recording with identical session-level timestamps/duration.
CHAPTER_FILENAME_PATTERN = re.compile(
    r"^(?:GOPR(?P<gopr_group>\d{4})"
    r"|G[A-Z](?P<chapter_num>\d{2})(?P<chapter_group>\d{4}))"
    r"\.(?P<extension>[A-Za-z0-9]+)$",
    re.IGNORECASE,
)


def parse_chapter_filename(filename: str) -> tuple[str, int] | None:
    match = CHAPTER_FILENAME_PATTERN.match(filename)
    if not match:
        return None
    if match.group("gopr_group"):
        return match.group("gopr_group"), 0
    return match.group("chapter_group"), int(match.group("chapter_num"))


def has_chapter_files(item: MediaItem) -> bool:
    try:
        return int(item.metadata.get("item_count") or 0) > 1
    except (TypeError, ValueError):
        return False


def build_chapter_directory_listing(output_dir: Path) -> list[Path]:
    """One-time scan of every file that could plausibly be a chapter (flat
    in output_dir, or under any original_unmerged_*/ subdir), for reuse
    across many items via find_chapter_source_files_from_listing instead of
    each item re-scanning the directory tree itself -- important for a
    dashboard computing merge status for every chaptered row on one page
    load."""
    if not output_dir.exists():
        return []
    search_dirs = [output_dir] + [
        candidate
        for candidate in output_dir.iterdir()
        if candidate.is_dir()
        and candidate.name.casefold().startswith(ORIGINAL_UNMERGED_FOLDER_PREFIX)
    ]
    listing: list[Path] = []
    for directory in search_dirs:
        for candidate in directory.iterdir():
            if candidate.is_file() and safe_child_path(output_dir, candidate):
                listing.append(candidate)
    return listing


def find_chapter_source_files_from_listing(
    listing: list[Path], item: MediaItem
) -> list[Path] | None:
    parsed_item = parse_chapter_filename(item.filename)
    if parsed_item is None:
        return None
    item_group, _ = parsed_item
    item_extension = Path(item.filename).suffix.casefold()

    # Keyed by casefolded filename so a chapter present in more than one
    # search_dirs entry (e.g. both flat and already in original_unmerged_*
    # from a half-completed prior run) is only counted once.
    candidates: dict[str, tuple[int, Path]] = {}
    for candidate in listing:
        if candidate.suffix.casefold() != item_extension:
            continue
        parsed_candidate = parse_chapter_filename(candidate.name)
        if parsed_candidate is None:
            continue
        candidate_group, chapter_number = parsed_candidate
        if candidate_group.casefold() != item_group.casefold():
            continue
        candidates[candidate.name.casefold()] = (chapter_number, candidate)

    if len(candidates) < 2:
        return None
    return [path for _, path in sorted(candidates.values())]


def find_chapter_source_files(output_dir: Path, item: MediaItem) -> list[Path] | None:
    if not output_dir.exists():
        return None
    return find_chapter_source_files_from_listing(
        build_chapter_directory_listing(output_dir), item
    )


# merge_status values for a chaptered (item_count > 1) media item, computed
# live from the filesystem -- never persisted to gosync_state.json, so
# there's nothing to go stale between dashboard polls.
MERGE_STATUS_MERGED = "merged"
MERGE_STATUS_SIZE_MISMATCH = "size_mismatch"
MERGE_STATUS_CHAPTERS_READY = "chapters_ready"
MERGE_STATUS_CHAPTERS_PARTIAL = "chapters_partial"
MERGE_STATUS_CHAPTERS_MISSING = "chapters_missing"

# A row's remerge checkbox may only be offered when there's actually
# something on disk to merge from.
MERGE_STATUS_REMERGE_ELIGIBLE = {
    MERGE_STATUS_MERGED,
    MERGE_STATUS_SIZE_MISMATCH,
    MERGE_STATUS_CHAPTERS_READY,
}


def compute_merge_status(
    item: MediaItem,
    target_path: Path,
    listing: list[Path],
) -> str:
    try:
        item_count = int(item.metadata.get("item_count") or 0)
    except (TypeError, ValueError):
        item_count = 0

    chapter_paths = find_chapter_source_files_from_listing(listing, item)
    chapters_complete = (
        item_count > 0
        and chapter_paths is not None
        and len(chapter_paths) == item_count
    )

    if target_path.exists():
        if not chapter_paths:
            # No local chapters left to validate against (e.g. the user
            # deleted original_unmerged_<ext>/ after confirming the merge
            # looked right) -- a perfectly normal end state, treated as
            # merged since there's nothing to compare against.
            return MERGE_STATUS_MERGED
        local_chapter_sum = sum(path.stat().st_size for path in chapter_paths)
        actual_size = target_path.stat().st_size
        if size_matches(actual_size, local_chapter_sum, item_count=len(chapter_paths)):
            return MERGE_STATUS_MERGED
        return MERGE_STATUS_SIZE_MISMATCH

    if chapters_complete:
        return MERGE_STATUS_CHAPTERS_READY
    if chapter_paths:
        return MERGE_STATUS_CHAPTERS_PARTIAL
    return MERGE_STATUS_CHAPTERS_MISSING


def tolerance_bytes(item_count: int) -> int:
    """Absolute-byte size tolerance for a chaptered merge, scaled by chapter
    count rather than total file size. Measured merge overhead (see
    scripts/measure_chapter_merge_size_delta.py) is roughly constant per
    chapter -- ffmpeg's concat consolidates each chapter's own container
    overhead (moov/ftyp boxes) into a single copy instead of N -- not
    proportional to total payload size. A percentage-of-size band was tried
    first and rejected: on a multi-GB file it left a multi-hundred-MB blind
    spot for genuine truncation, while being needlessly tight on small clips.
    """
    return min(
        max(item_count * PER_CHAPTER_OVERHEAD_BYTES, MIN_TOLERANCE_BYTES),
        MAX_TOLERANCE_BYTES,
    )


def size_matches(
    actual_size: int, expected_size: int | None, item_count: int = 1
) -> bool:
    # expected_size is only known when the API reported a file_size (or a
    # caller supplied one, e.g. a local chapter-byte sum); without it we
    # can't detect a mismatch, so fall back to existence alone.
    if expected_size is None:
        return True
    if item_count <= 1:
        # A plain, non-chaptered download is extracted from the zip
        # unmodified -- any size deviation is real truncation/corruption,
        # never a legitimate remux difference, so no tolerance applies.
        return actual_size == expected_size
    return abs(actual_size - expected_size) <= tolerance_bytes(item_count)


def is_near_tolerance_boundary(
    actual_size: int,
    expected_size: int | None,
    item_count: int,
    threshold: float = 0.2,
) -> bool:
    """True if actual_size is within `threshold` of the tolerance limit for
    item_count but still (per size_matches) considered valid. Used to emit
    an early-warning event when real-world merge overhead is drifting
    towards the configured tolerance, before it ever causes a false
    rejection -- the tolerance constants were measured once, on one
    library/camera/ffmpeg build, and have no other way to signal drift."""
    if expected_size is None or item_count <= 1:
        return False
    if not size_matches(actual_size, expected_size, item_count):
        return False
    limit = tolerance_bytes(item_count)
    if limit <= 0:
        return False
    delta = abs(actual_size - expected_size)
    return delta >= limit * (1 - threshold)


def validate_chapter_integrity(chapter_paths: list[Path]) -> bool:
    """Sanity-check chapter files before trusting them as merge inputs or as
    the size oracle for a corruption check. Chapter *presence* (an exact
    item_count match) says nothing about chapter *correctness* -- a
    truncated/zero-byte chapter file still counts as "present" by existence
    alone, and would otherwise poison both the merge output and the sum a
    corruption check validates against."""
    if not chapter_paths:
        return False
    sizes: list[int] = []
    for path in chapter_paths:
        try:
            sizes.append(path.stat().st_size)
        except OSError:
            return False
    if any(size <= 0 for size in sizes):
        return False
    median_size = sorted(sizes)[len(sizes) // 2]
    if median_size <= 0:
        return False
    # A real chapter is never a tiny fraction of its siblings' size; a
    # truncated/near-empty chapter is exactly the failure mode a bare
    # existence/count check can't catch.
    if any(size < median_size * 0.1 for size in sizes):
        return False

    ffprobe_binary = shutil.which(FFPROBE_BINARY)
    if not ffprobe_binary:
        # No ffprobe available -- the size-outlier check above is the best
        # we can do; don't fail the whole recording over a missing binary.
        return True
    for path in chapter_paths:
        try:
            result = subprocess.run(
                [
                    ffprobe_binary,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 or not result.stdout.strip():
            return False
    return True
