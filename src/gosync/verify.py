"""Check whether a chapter-merged file kept everything its source chapters
had: same stream layout (video/audio/data tracks like GoPro's gpmd/tmcd),
the same global container metadata, a duration matching the sum of the
chapters' durations, exact per-stream packet counts, a byte-exact gpmd
telemetry payload, and that it's actually playable (spot-decoded at the
start, end, and each old chapter-boundary splice point).

Source chapters are recovered via gosync.chapters.find_chapter_source_files
(the same widened chapter-finder process_pipeline uses), which looks in
output_dir flat and under original_unmerged_<ext>/, where merge_chapter_files()
in gosync.downloader moves them after a successful merge. Nothing here
modifies the downloads folder.

With no --filename, reads data/manifest.json, finds every media item with
metadata.item_count > 1 (i.e. every chapter-style recording GoSync knows
about), and verifies each one that actually has both a merged file under
downloads/<ext>/ and its source chapters under downloads/original_unmerged_
<ext>/ on disk -- items not downloaded/merged locally yet are reported as
skipped, not treated as failures.

An HTML summary report is always written (default: <data-dir>/reports/
chapter-merge-verify.html), in addition to the console output.

Usage:
    gosync-verify-chapters                          # verify all chaptered
                                                     # items in manifest.json
    gosync-verify-chapters --filename GX010320.MP4  # verify just one
    gosync-verify-chapters --data-dir /path/to/data
    gosync-verify-chapters --html-path /custom/report.html
    gosync-verify-chapters --stage-locally          # copy each file to local
                                                     # disk once instead of
                                                     # re-reading it over the
                                                     # network multiple times
                                                     # (use when --output-dir
                                                     # is a network mount)
"""

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gosync.chapters import find_chapter_source_files
from gosync.config import DEFAULT_DATA_DIR
from gosync.paths import media_download_path

DURATION_TOLERANCE_RATIO = 0.02  # 2%: concat container overhead is tiny

# -count_packets and the raw-stream extract both fully demux the file, so on
# multi-GB GoPro chapter merges (tens of GB) read over a network share (e.g.
# SMB), the read alone can take well beyond a "generous" 10-minute budget.
FULL_READ_TIMEOUT_SECONDS = 3600
SPOT_CHECK_TIMEOUT_SECONDS = 300

# ffmpeg's MP4 muxer always regenerates these itself, independent of
# -map_metadata -- they differ (or gain an "encoder" tag) on every remux, so
# comparing them against the source chapter is not a meaningful check.
MUXER_OWNED_TAGS = {"major_brand", "minor_version", "compatible_brands", "encoder"}

# tmcd (timecode) tracks carry exactly one packet per file by design -- a
# single start-timecode + track-duration marker, not one packet per frame.
# Each chapter's tmcd packet describes that chapter's own standalone
# duration, which is meaningless once merged; the merged file correctly ends
# up with one tmcd packet covering the full duration instead of N. So
# sum(chapters) != merged is expected here and isn't data loss.
PACKET_COUNT_EXEMPT_TAGS = {"tmcd"}

# GoPro's proprietary telemetry (GPS/accel/gyro/etc.) is muxed as a "gpmd"
# data track. This project has no local GPMF decoder (telemetry.py only
# fetches GPX/GPMF sidecars from GoPro's cloud API, not from the mp4 itself,
# and no GPMF-parsing library is in requirements.txt), so payload integrity
# is checked at the byte level instead of being semantically decoded.
TELEMETRY_CODEC_TAG = "gpmd"


def find_chapter_paths(output_dir: Path, filename: str) -> list[Path]:
    """Thin adapter over gosync.chapters.find_chapter_source_files, which
    takes a MediaItem-like object rather than a bare filename -- only
    .filename is actually read for chapter discovery, so a lightweight
    stand-in is enough here."""
    pseudo_item = SimpleNamespace(filename=filename)
    return find_chapter_source_files(output_dir, pseudo_item) or []


def ffprobe(path: Path) -> dict[str, Any]:
    # -count_packets forces a full demux (not just header reads) so each
    # stream's nb_read_packets is exact -- needed to prove the -c copy concat
    # didn't drop or duplicate packets at a chapter boundary.
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-count_packets",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=FULL_READ_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed on {path}:\n{result.stderr}")
    return json.loads(result.stdout)


def packet_counts(probe: dict[str, Any]) -> dict[tuple[str, str], int]:
    # Keyed by (codec_type, codec_tag_string) rather than stream index: the
    # merge can reorder streams (observed video/audio/tmcd/gpmd -> video/
    # audio/gpmd/tmcd in practice), so index alignment isn't safe.
    counts: dict[tuple[str, str], int] = {}
    for s in probe.get("streams", []):
        key = (s.get("codec_type", ""), s.get("codec_tag_string", ""))
        counts[key] = counts.get(key, 0) + int(s.get("nb_read_packets") or 0)
    return counts


def find_stream_index(probe: dict[str, Any], codec_tag: str) -> int | None:
    return next(
        (
            s.get("index")
            for s in probe.get("streams", [])
            if s.get("codec_tag_string") == codec_tag
        ),
        None,
    )


def extract_raw_stream(
    path: Path, stream_index: int, tmp_dir: Path, label: str
) -> bytes:
    # -f data with -c copy dumps each packet's payload back-to-back with no
    # container framing, so this is the raw GPMF byte stream as GoPro wrote
    # it -- comparable byte-for-byte across files.
    out_path = tmp_dir / f"{label}.bin"
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-i", str(path),
            "-map", f"0:{stream_index}",
            "-c", "copy",
            "-f", "data",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=FULL_READ_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"ffmpeg raw stream extract failed on {path} stream {stream_index}:\n"
            f"{result.stderr}"
        )
    return out_path.read_bytes()


SPOT_CHECK_WINDOW_SECONDS = 5.0


def decode_window(path: Path, start: float, length: float) -> tuple[bool, str]:
    # -xerror aborts on the first decode error instead of ffmpeg's default
    # behavior of concealing corrupt frames/packets and continuing, which
    # would otherwise hide corruption from a plain exit-code check.
    # -ss before -i is fast input seeking; -f null - decodes without writing
    # output, so this proves playability of the window without the cost of
    # decoding the whole file.
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-xerror",
            "-ss", f"{max(start, 0.0):.2f}",
            "-i", str(path),
            "-t", f"{length:.2f}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        timeout=SPOT_CHECK_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True, "clean"
    stderr_tail = "\n".join(result.stderr.strip().splitlines()[-5:])
    return False, f"exit {result.returncode}: {stderr_tail}"


def spot_check_playability(
    path: Path, duration: float, boundaries: list[float]
) -> tuple[bool, str]:
    """Decode short windows at the start, end, and each old chapter-boundary
    splice point instead of the whole file -- those splice points are where
    concat corruption would actually show up, so this catches the same class
    of defect as a full decode in a fraction of the time."""
    half = SPOT_CHECK_WINDOW_SECONDS / 2
    windows = {"start": 0.0}
    for i, boundary in enumerate(boundaries, start=1):
        windows[f"boundary {i} (~{boundary:.1f}s)"] = max(boundary - half, 0.0)
    if duration > SPOT_CHECK_WINDOW_SECONDS:
        windows["end"] = duration - SPOT_CHECK_WINDOW_SECONDS

    failures = {}
    for label, start in windows.items():
        ok, detail = decode_window(path, start, SPOT_CHECK_WINDOW_SECONDS)
        if not ok:
            failures[label] = detail

    if not failures:
        return True, f"{len(windows)} window(s) decoded cleanly: {', '.join(windows)}"
    return False, "; ".join(f"{label}: {detail}" for label, detail in failures.items())


def payload_mismatch_detail(expected: bytes, actual: bytes) -> str:
    first_diff = next(
        (
            i
            for i in range(min(len(expected), len(actual)))
            if expected[i] != actual[i]
        ),
        min(len(expected), len(actual)),
    )
    return (
        f"expected {len(expected):,} bytes (concatenated chapters), got "
        f"{len(actual):,} bytes in merged (delta={len(actual) - len(expected):+,}); "
        f"first byte difference at offset {first_diff:,}"
    )


def stream_signature(probe: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (
            s.get("codec_type", ""),
            s.get("codec_tag_string", ""),
            s.get("codec_name", ""),
        )
        for s in probe.get("streams", [])
    ]


def stage_local_copy(path: Path, scratch_dir: Path, label: str) -> Path:
    """Copy `path` into `scratch_dir` once, so every later ffprobe/ffmpeg pass
    (count_packets, raw-stream extract, decode spot-checks -- three-plus
    reads of the same bytes) hits local disk instead of re-reading a slow
    mount (e.g. SMB) each time."""
    scratch_dir.mkdir(parents=True, exist_ok=True)
    local_path = scratch_dir / path.name
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"  staging {label} locally ({size_mb:,.0f} MB)... ", end="", flush=True)
    started = time.monotonic()
    shutil.copyfile(path, local_path)
    print(f"done ({time.monotonic() - started:.1f}s)", flush=True)
    return local_path


def probe_with_progress(path: Path, label: str) -> dict[str, Any]:
    size_mb = path.stat().st_size / (1024 * 1024)
    print(
        f"  probing {label} ({size_mb:,.0f} MB, full demux for packet counts)... ",
        end="",
        flush=True,
    )
    started = time.monotonic()
    probe = ffprobe(path)
    print(f"done ({time.monotonic() - started:.1f}s)", flush=True)
    return probe


def report(
    label: str, ok: bool, detail: str, checks: list[tuple[str, str, str]] | None = None
) -> bool:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {label}: {detail}")
    if checks is not None:
        checks.append((label, status, detail))
    return ok


def discover_chaptered_filenames(manifest_path: Path) -> list[tuple[str, int]]:
    """Every manifest media item with metadata.item_count > 1, i.e. every
    chapter-style GoPro recording -- (filename, item_count), in manifest
    order. Reads data/manifest.json directly since gosync.manifest has no
    reader back from that file into MediaItem objects (write_manifest() is
    the only manifest.json writer; app.py/state.py consume it as raw JSON)."""
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = []
    for entry in manifest.get("media", []):
        item_count = int((entry.get("metadata") or {}).get("item_count") or 1)
        if item_count > 1:
            result.append((entry["filename"], item_count))
    return result


def verify_one(
    filename: str, output_dir: Path, scratch_dir: Path | None = None
) -> dict[str, Any]:
    """Run all checks for one chaptered filename. Returns a result dict with
    status True/False/None (pass/fail/not-verifiable-yet), the list of
    individual (label, PASS|FAIL, detail) checks that ran, and the number of
    chapter files found.

    If scratch_dir is given, the merged file and every chapter are first
    copied there (one network read each) and all checks run against those
    local copies instead -- avoiding the 3-4 separate full-file re-reads
    (count_packets, raw-stream extract, decode spot-checks) the checks below
    would otherwise make against a slow/network-mounted output_dir."""
    checks: list[tuple[str, str, str]] = []
    chapter_paths = find_chapter_paths(output_dir, filename)
    merged_path = media_download_path(output_dir, filename)

    if len(chapter_paths) < 2 or not merged_path.exists():
        if len(chapter_paths) < 2:
            skip_reason = (
                f"only {len(chapter_paths)} chapter file(s) on disk under "
                f"{output_dir} (checked flat + original_unmerged_* folders)"
            )
        else:
            skip_reason = f"merged file not found at {merged_path}"
        print(f"[SKIP] {filename}: {skip_reason}")
        return {
            "status": None,
            "checks": checks,
            "chapter_count": len(chapter_paths),
            "skip_reason": skip_reason,
        }

    print(f"Merged file: {merged_path}")
    print("Source chapters (merge order):")
    for path in chapter_paths:
        print(f"  {path}")
    print()

    item_scratch_dir: Path | None = None
    try:
        if scratch_dir is not None:
            item_scratch_dir = Path(
                tempfile.mkdtemp(prefix=f"{Path(filename).stem}_", dir=scratch_dir)
            )
            print(
                f"Staging {1 + len(chapter_paths)} file(s) to local scratch "
                f"({item_scratch_dir}):"
            )
            merged_path = stage_local_copy(merged_path, item_scratch_dir, "merged file")
            chapter_paths = [
                stage_local_copy(
                    path, item_scratch_dir, f"chapter {i}/{len(chapter_paths)}"
                )
                for i, path in enumerate(chapter_paths, start=1)
            ]
            print()

        return _run_checks(merged_path, chapter_paths, checks)
    finally:
        if item_scratch_dir is not None:
            shutil.rmtree(item_scratch_dir, ignore_errors=True)


def _run_checks(
    merged_path: Path, chapter_paths: list[Path], checks: list[tuple[str, str, str]]
) -> dict[str, Any]:
    print(
        f"Probing {1 + len(chapter_paths)} file(s): "
        f"1 merged + {len(chapter_paths)} chapter(s):"
    )
    merged_probe = probe_with_progress(merged_path, "merged file")
    chapter_probes = [
        probe_with_progress(path, f"chapter {i}/{len(chapter_paths)}")
        for i, path in enumerate(chapter_paths, start=1)
    ]
    print()

    all_ok = True

    # 1. Stream layout: merged should have the same tracks (video/audio/data,
    # e.g. GoPro's gpmd telemetry and tmcd timecode) as the source chapters.
    # -ignore_unknown in the merge command can silently drop data streams
    # ffmpeg doesn't recognize, which docs/download-items.md claims doesn't
    # happen -- this is the check that would catch it.
    reference_signature = stream_signature(chapter_probes[0])
    merged_signature = stream_signature(merged_probe)
    missing = [s for s in reference_signature if s not in merged_signature]
    all_ok &= report(
        "stream layout",
        not missing,
        "merged has all source streams"
        if not missing
        else f"merged is missing streams present in chapter 1: {missing}",
        checks=checks,
    )
    print(f"    chapter 1 streams: {reference_signature}")
    print(f"    merged streams:    {merged_signature}")

    # 2. Global container metadata (camera model, firmware, lens, etc. via
    # -map_metadata 0 -movflags use_metadata_tags) should carry over from
    # chapter 1 onto the merged file.
    reference_tags = chapter_probes[0].get("format", {}).get("tags", {})
    merged_tags = merged_probe.get("format", {}).get("tags", {})
    dropped_tags = {
        k: v
        for k, v in reference_tags.items()
        if k not in MUXER_OWNED_TAGS and merged_tags.get(k) != v
    }
    all_ok &= report(
        "container metadata",
        not dropped_tags,
        "all chapter-1 content tags present in merged"
        if not dropped_tags
        else (
            "tags missing/changed in merged (not muxer-regenerated ones): "
            f"{dropped_tags}"
        ),
        checks=checks,
    )

    # 3. Duration should be ~sum of the chapters' actual encoded durations,
    # not the single chapter duration GoPro stamps into every chapter's own
    # metadata (docs/download-items.md notes all chapters share one Duration
    # tag, so this must be measured from ffprobe, not read from tags).
    def duration_of(probe: dict[str, Any]) -> float:
        return float(probe.get("format", {}).get("duration", 0.0))

    chapter_duration_sum = sum(duration_of(p) for p in chapter_probes)
    merged_duration = duration_of(merged_probe)
    duration_delta = abs(merged_duration - chapter_duration_sum)
    duration_ok = (
        chapter_duration_sum > 0
        and duration_delta <= chapter_duration_sum * DURATION_TOLERANCE_RATIO
    )
    all_ok &= report(
        "duration",
        duration_ok,
        f"merged={merged_duration:.2f}s vs "
        f"sum(chapters)={chapter_duration_sum:.2f}s (delta={duration_delta:.2f}s, "
        f"tolerance={chapter_duration_sum * DURATION_TOLERANCE_RATIO:.2f}s)",
        checks=checks,
    )

    # 4. Exact per-stream packet counts: since the merge is -c copy (no
    # re-encoding), every packet from every chapter must land in the merged
    # file untouched. Unlike the duration check, this is an exact equality,
    # not a tolerance -- it's the real proof against dropped/duplicated
    # frames at a chapter splice point.
    chapter_packet_totals: dict[tuple[str, str], int] = {}
    for probe in chapter_probes:
        for key, count in packet_counts(probe).items():
            chapter_packet_totals[key] = chapter_packet_totals.get(key, 0) + count
    merged_packet_totals = packet_counts(merged_probe)

    packet_mismatches = {
        key: (expected, merged_packet_totals.get(key, 0))
        for key, expected in chapter_packet_totals.items()
        if key[1] not in PACKET_COUNT_EXEMPT_TAGS
        and merged_packet_totals.get(key, 0) != expected
    }
    all_ok &= report(
        "packet counts",
        not packet_mismatches,
        "merged packet count matches sum(chapters) exactly, per stream"
        if not packet_mismatches
        else "mismatch (stream -> sum(chapters), merged): " + ", ".join(
            f"{key}: {expected} vs {actual}"
            for key, (expected, actual) in packet_mismatches.items()
        ),
        checks=checks,
    )

    # 5. Payload-level check on GoPro's gpmd telemetry track: extract the raw
    # stream bytes (not just packet counts) from each chapter and from the
    # merged file, and require the merged payload to be an exact byte-for-
    # byte concatenation of the chapters' payloads in order. This is the
    # strongest available check without a GPMF decoder -- it catches
    # corruption or truncation inside the telemetry data itself, which a
    # packet-count match alone would not.
    merged_gpmd_index = find_stream_index(merged_probe, TELEMETRY_CODEC_TAG)
    chapter_gpmd_indices = [
        find_stream_index(p, TELEMETRY_CODEC_TAG) for p in chapter_probes
    ]
    if merged_gpmd_index is None or any(idx is None for idx in chapter_gpmd_indices):
        print("[SKIP] gpmd telemetry payload: no gpmd track on one or more files")
        checks.append(
            ("gpmd telemetry payload", "SKIP", "no gpmd track on one or more files")
        )
    else:
        print(
            "  extracting gpmd payload for byte-exact comparison... ",
            end="",
            flush=True,
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            chapter_payload = b"".join(
                extract_raw_stream(path, idx, tmp_dir, f"chapter_{i}")
                for i, (path, idx) in enumerate(
                    zip(chapter_paths, chapter_gpmd_indices, strict=False), start=1
                )
            )
            merged_payload = extract_raw_stream(
                merged_path, merged_gpmd_index, tmp_dir, "merged"
            )
        print(f"done ({time.monotonic() - started:.1f}s)", flush=True)

        payload_ok = merged_payload == chapter_payload
        all_ok &= report(
            "gpmd telemetry payload",
            payload_ok,
            (
                f"{len(merged_payload):,} bytes, byte-exact match with "
                "concatenated chapters"
            )
            if payload_ok
            else payload_mismatch_detail(chapter_payload, merged_payload),
            checks=checks,
        )

    # 6. Playability spot-check: decode short windows at the start, end, and
    # each old chapter-boundary splice point with -xerror (aborts on the
    # first decode error instead of silently concealing it). Packet counts/
    # byte-exact payloads (checks 4-5) can't catch corruption *inside* a
    # packet's payload that leaves its size unchanged -- decoding does, and
    # targeting the splice points catches what a full decode would, fast.
    boundaries = []
    running_total = 0.0
    for probe in chapter_probes[:-1]:
        running_total += duration_of(probe)
        boundaries.append(running_total)

    print(
        "  spot-checking playability at start/end/chapter-boundaries... ",
        end="",
        flush=True,
    )
    started = time.monotonic()
    playable_ok, playable_detail = spot_check_playability(
        merged_path, merged_duration, boundaries
    )
    print(f"done ({time.monotonic() - started:.1f}s)", flush=True)
    all_ok &= report(
        "playability spot-check", playable_ok, playable_detail, checks=checks
    )

    print()
    print(
        "All checks passed."
        if all_ok
        else "Some checks failed -- see FAIL lines above."
    )
    return {
        "status": all_ok,
        "checks": checks,
        "chapter_count": len(chapter_paths),
        "skip_reason": None,
    }


HTML_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>GoSync chapter-merge verification</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem;
         background: #0b0f14; color: #d8dee9; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .meta {{ color: #7c8896; font-size: 0.85rem; margin-bottom: 1.5rem; }}
  .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .card {{ background: #131a22; border: 1px solid #223; border-radius: 8px;
          padding: 0.75rem 1.25rem; min-width: 140px; }}
  .card .n {{ font-size: 1.6rem; font-weight: 600; }}
  .card .l {{ font-size: 0.75rem; color: #7c8896; text-transform: uppercase; }}
  .card.warn .n {{ color: #e5a34a; }}
  .card.bad .n {{ color: #e5605a; }}
  .card.good .n {{ color: #5ac68a; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem;
           border-bottom: 1px solid #1c242c; }}
  th {{ position: sticky; top: 0; background: #0b0f14; }}
  tr.status-fail {{ background: #2a1414; }}
  tr.status-skip {{ color: #7c8896; }}
  .pill {{ padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.75rem; }}
  .pill.pass {{ background: #123423; color: #5ac68a; }}
  .pill.fail {{ background: #3a1414; color: #e5605a; }}
  .pill.skip {{ background: #3a2c12; color: #e5a34a; }}
  .mono {{ font-variant-numeric: tabular-nums; }}
  .dim {{ color: #7c8896; }}
  .toggle {{ cursor: pointer; display: inline-block; width: 1rem; color: #7c8896;
            user-select: none; }}
  .toggle.hidden {{ visibility: hidden; }}
  tr.detail-row td {{ background: #0e141b; padding: 0.5rem 0.6rem 0.75rem 2.5rem; }}
  table.checks {{ width: auto; min-width: 480px; }}
  table.checks th, table.checks td {{ padding: 0.2rem 0.75rem 0.2rem 0; border: none;
                                      font-size: 0.8rem; vertical-align: top; }}
  table.checks th {{ position: static; color: #7c8896; font-weight: 500; }}
  .skip-reason {{ color: #7c8896; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>GoSync chapter-merge verification</h1>
<div class="meta">Generated {generated_at}</div>

<div class="cards">
  <div class="card">
    <div class="n">{total}</div><div class="l">Files checked</div>
  </div>
  <div class="card good">
    <div class="n">{passed}</div><div class="l">Passed</div>
  </div>
  <div class="card {fail_class}">
    <div class="n">{failed}</div><div class="l">Failed</div>
  </div>
  <div class="card warn">
    <div class="n">{skipped}</div><div class="l">Skipped (not on disk)</div>
  </div>
</div>

<table id="report">
  <thead>
    <tr>
      <th></th>
      <th>Filename</th>
      <th>Chapters</th>
      <th>Status</th>
      <th>Checks</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>

<script>
function toggleDetail(el) {{
  const detail = el.closest('tr').nextElementSibling;
  if (!detail || !detail.classList.contains('detail-row')) return;
  const open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : '';
  el.textContent = open ? '▸' : '▾';
}}
</script>
</body>
</html>
"""

STATUS_LABELS = {True: "pass", False: "fail", None: "skip"}


def render_check_rows(checks: list[tuple[str, str, str]]) -> str:
    if not checks:
        return '<tr><td colspan="2" class="dim">no checks ran</td></tr>'
    rows = []
    for label, status, detail in checks:
        pill_class = status.lower()
        rows.append(
            f'    <tr><td class="mono">{html.escape(label)}</td>'
            f'<td><span class="pill {pill_class}">{status}</span> '
            f'{html.escape(detail)}</td></tr>'
        )
    return "\n".join(rows)


def render_html_report(results: dict[str, dict[str, Any]]) -> str:
    passed = sum(1 for r in results.values() if r["status"] is True)
    failed = sum(1 for r in results.values() if r["status"] is False)
    skipped = sum(1 for r in results.values() if r["status"] is None)

    rows = []
    for filename, result in results.items():
        status_label = STATUS_LABELS[result["status"]]
        checks = result["checks"]
        pass_count = sum(1 for _, s, _ in checks if s == "PASS")
        fail_count = sum(1 for _, s, _ in checks if s == "FAIL")
        skip_reason_html = html.escape(result.get("skip_reason") or "")
        checks_summary = (
            f"{pass_count} passed, {fail_count} failed"
            if checks
            else f'<span class="skip-reason">{skip_reason_html}</span>'
        )
        rows.append(
            f'    <tr class="status-{status_label}">'
            f'<td><span class="toggle" onclick="toggleDetail(this)">▸</span></td>'
            f'<td class="mono">{html.escape(filename)}</td>'
            f'<td class="mono">{result["chapter_count"]}</td>'
            f'<td><span class="pill {status_label}">{status_label.upper()}</span></td>'
            f'<td>{checks_summary}</td></tr>'
        )
        checks_table_head = (
            '<table class="checks"><thead><tr><th>Check</th>'
            "<th>Result</th></tr></thead>"
        )
        rows.append(
            f'    <tr class="detail-row" style="display:none"><td></td><td colspan="4">'
            f'{checks_table_head}'
            f'<tbody>\n{render_check_rows(checks)}\n    </tbody></table>'
            f'</td></tr>'
        )

    return HTML_PAGE_TEMPLATE.format(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        total=len(results),
        passed=passed,
        failed=failed,
        skipped=skipped,
        fail_class="bad" if failed else "",
        rows="\n".join(rows),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=Path(DEFAULT_DATA_DIR))
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest.json to read when --filename is omitted "
        "(default: <data-dir>/manifest.json)",
    )
    parser.add_argument(
        "--filename",
        default=None,
        help="verify just this one file, e.g. GX010320.MP4 (default: verify every "
        "chaptered item found in manifest.json)",
    )
    parser.add_argument(
        "--html-path",
        type=Path,
        default=None,
        help="path for the HTML summary report, always written "
        "(default: <data-dir>/reports/chapter-merge-verify.html)",
    )
    parser.add_argument(
        "--stage-locally",
        action="store_true",
        help="copy each merged/chapter file to local scratch disk once before "
        "verifying it, instead of letting ffprobe/ffmpeg re-read it from "
        "--output-dir 3-4 times. Use this when --output-dir is a slow/network "
        "mount (e.g. SMB) -- it roughly halves total bytes transferred over "
        "the network. Needs local scratch space comparable to one item's "
        "total size (merged file + one chapter).",
    )
    parser.add_argument(
        "--scratch-dir",
        type=Path,
        default=None,
        help="local disk directory for --stage-locally copies (default: "
        "<data-dir>/.verify_scratch). Must be real local disk, not a tmpfs "
        "or another network mount, or staging defeats its own purpose.",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir or (data_dir / "downloads")
    html_path = args.html_path or (data_dir / "reports" / "chapter-merge-verify.html")
    scratch_dir = None
    if args.stage_locally:
        scratch_dir = (args.scratch_dir or (data_dir / ".verify_scratch")).resolve()

    results: dict[str, dict[str, Any]] = {}

    if args.filename:
        results[args.filename] = verify_one(args.filename, output_dir, scratch_dir)
        exit_ok = results[args.filename]["status"]
    else:
        manifest_path = args.manifest or (data_dir / "manifest.json")
        chaptered = discover_chaptered_filenames(manifest_path)
        print(f"{len(chaptered)} chaptered item(s) in {manifest_path}\n")

        for i, (filename, item_count) in enumerate(chaptered, start=1):
            print(f"=== [{i}/{len(chaptered)}] {filename} ({item_count} chapters) ===")
            results[filename] = verify_one(filename, output_dir, scratch_dir)
            print()

        passed = [f for f, r in results.items() if r["status"] is True]
        failed = [f for f, r in results.items() if r["status"] is False]
        skipped = [f for f, r in results.items() if r["status"] is None]

        print("=== Summary ===")
        print(f"  {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped "
              f"(not on disk yet), out of {len(chaptered)} chaptered manifest item(s)")
        if failed:
            print("  FAILED: " + ", ".join(failed))
        if skipped:
            print("  SKIPPED: " + ", ".join(skipped))
        exit_ok = not failed

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html_report(results), encoding="utf-8")
    print(f"\nHTML report written to {html_path}")

    sys.exit(0 if exit_ok else 1)


if __name__ == "__main__":
    main()
