# Media Items

This document explains how GoSync turns GoPro Cloud HAR data into local
download records, how those records move through state and extraction, and
how GoPro chapter files are merged into a single file.

## HAR Source

GoSync reads media data from exported browser HAR entries whose request URL
contains:

```text
https://api.gopro.com/media/search
```

For each matching HAR entry, GoSync parses `response.content.text` as JSON and
looks for media arrays such as `_embedded.media`. Each media object is treated
as a candidate download item when it has a usable filename and extension and is
not a known non-media type such as an album, folder, page, profile, summary, or
pagination object.

HAR files can contain more than one `media/search` response. GoSync combines
the discovered media objects into one local manifest and keeps the raw media
objects in `media_search.json` for inspection.

## Manifest Items

`manifest.json` is the deduplicated list of media records GoSync uses for
downloads, sidecars, and UI rows. A manifest item contains:

```text
key
media_id
filename
sidecar_filename
file_size
metadata
```

The `key` is built from the media ID and filename. This lets GoSync distinguish
records that might share a filename or appear in multiple HAR responses.

`metadata` is the original media object from the HAR after GoSync has selected
it as a media file. It can include fields such as camera model, capture time,
resolution, dimensions, duration, content type, file extension, and cloud
availability labels. Some fields are used for sidecar generation, while others
are retained for reports and troubleshooting.

When `metadata.item_count` is greater than `1`, GoSync treats the manifest item
as a recording that may download with chapter files. Those items are downloaded
one manifest item per batch so any extra physical files in the zip can be
merged into the manifest item's single target file immediately after
extraction (see [Chapter Files And Merging](#chapter-files-and-merging) below).

If GoSync sees the same media ID and filename more than once, it keeps the first
manifest item and records later occurrences as duplicates.

## State Records

`gosync_state.json` stores resumable state for every manifest item. Each state
record contains:

```text
key
id
filename
sidecar_filename
file_size
download_status
sidecar_status
retry_count
last_error
```

The download status belongs to the manifest item, not necessarily every
physical file that may be extracted from a zip. This distinction matters for
chapter files.

On startup and before download jobs, GoSync syncs state with the actual
`downloads/` folder. It accepts files in canonical extension folders and legacy
flat files directly under `downloads/`, using case-insensitive filename checks
where needed. A file only counts as downloaded when it exists **and** its
on-disk size passes the tolerant size check described in [Verifying a merged
file's size](#verifying-a-merged-files-size) below.

## Download Batches

GoSync downloads pending manifest items through the GoPro zip endpoint:

```text
https://api.gopro.com/media/x/zip/source?ids=...
```

The IDs in the request are manifest media IDs. The response is a zip archive.
After download, GoSync validates the zip, safely extracts it into the download
folder, organizes extracted files into extension folders, and only then marks
the manifest items as downloaded.

Manifest items with `item_count` greater than `1` are always downloaded in their
own batch. Regular manifest items are packed by size and optional
files-per-batch limits.

The expected target path for a manifest item is:

```text
downloads/<extension>/<manifest-filename>
```

Extension folder names are lowercase. The stored filename keeps the manifest
filename casing when the file can be matched to a manifest item.

## Case Variants

GoPro zip members can differ from the HAR filename only by case. For example,
the HAR may use an uppercase extension while the zip member uses a lowercase
extension. GoSync treats these as the same manifest item, moves the extracted
file into the expected extension folder, and renames it to the manifest
filename.

This case-insensitive match is used only for filenames. Path traversal and
output directory safety checks still run before files are moved.

## Chapter Files And Merging

GoPro cameras split long recordings into multiple physical files ("chapters"),
each capped at a few GB. The HAR may describe the recording as one manifest
item, while the zip download contains more than one media file for that
recording (e.g. a 3-chapter recording yields 3 physical files). Downloaded
individually you'd end up with several disconnected clips instead of one
continuous video, so GoSync detects these chapter groups and merges them into
the manifest item's single target file.

### Detecting and ordering chapters

A manifest item is treated as chaptered when its `item_count` metadata field
is greater than 1. GoPro encodes chapter order directly in the filename:

- Modern cameras: `G[HX]<chapter 2-digit><group 4-digit>.<ext>` -- chapter
  number ascending is play order, e.g. chapter `01` then chapter `02` of the
  same group.
- Legacy cameras: `GOPR<group 4-digit>.<ext>` is chapter 1, followed by
  `G[PH]<chapter 2-digit><group 4-digit>.<ext>` for later chapters.

Embedded MP4/QuickTime metadata cannot be used for ordering or to tell
chapters apart: GoPro stamps every chapter of a recording with identical
session-level `Create Date`, `Duration`, camera/firmware/lens fields, and
`Media UID` prefix. The only per-chapter differences are the filename and
each chapter's actual encoded payload size.

After extraction, sibling chapter files are found by matching the same group
id and file extension as the manifest item's filename (guarding against
`.LRV`/`.THM` proxy/thumbnail files that reuse the same numbering pattern),
sorted ascending by chapter number. Fewer than 2 matching siblings on disk
means no merge is attempted.

### Merging the video streams

Chapters are concatenated with `ffmpeg`'s concat demuxer using stream copy
(`-c copy`, no re-encoding):

```
ffmpeg -y -f concat -safe 0 -i <chapter-list> -map 0 -ignore_unknown \
  -c copy -map_metadata 0 -movflags use_metadata_tags <output>
```

- `-map 0` carries every stream through untouched -- not just video/audio,
  but also GoPro's `gpmd` telemetry stream and `tmcd` timecode stream, which
  ffmpeg's default "best stream" selection would otherwise silently drop.
- `-ignore_unknown` drops only streams ffmpeg genuinely cannot classify
  (GoPro MP4s contain one such stream) instead of aborting the whole merge.
- If `ffmpeg` isn't installed, or the merge subprocess fails for any reason,
  the chapters are left as separate files on disk rather than losing data --
  merging is best-effort, never destructive. GoSync falls back to moving the
  manifest filename's chapter into the expected extension folder and moving
  the remaining chapter files alongside it as separate files, logging a
  warning or error so the fallback is visible.
- On success, the original chapter files are moved (not deleted) into an
  `original_unmerged_<ext>/` folder, so the raw per-chapter footage stays
  recoverable, and the manifest item is marked downloaded like any other
  single-file item.
- While the merge is running, GoSync reports a `Merging` status (CLI log
  line and web dashboard) so long-running `ffmpeg` merges are visible
  instead of appearing to hang.

Extra chapter files never appear as separate rows in `manifest.json`,
`gosync_state.json`, or the web UI -- the parent manifest item keeps the
download status and retry state, preserving all physical files from the zip
without inventing metadata that was not present as a separate HAR media
record.

#### Why `-ignore_unknown` is required

Real GoPro chapter files contain a stream ffmpeg cannot classify at all:

| Stream | Type | Codec |
|---|---|---|
| video | Video | HEVC (GoPro H.265) |
| audio | Audio | AAC |
| *(unidentified)* | -- | `Unknown: none` |
| telemetry | Data | `gpmd` (GoPro telemetry) |

With plain `-map 0` (no `-ignore_unknown`), ffmpeg refuses to mux the
unidentified stream into an MP4 container (`Cannot map stream ... -
unsupported type`) and the **entire merge job aborts** before writing
anything -- not just that one stream. `-ignore_unknown` (ffmpeg's own
suggested fix in that error message) drops only the genuinely
unclassifiable stream while still correctly mapping video, audio, and the
`gpmd` telemetry stream. There's nothing useful to preserve from the
unknown stream in the first place, since ffmpeg cannot identify what it
contains.

Verified stream-copy throughput on real footage: ~37x realtime
(I/O-bound, as expected for a copy with no re-encoding).

### Camera/lens metadata does not survive the ffmpeg remux

Testing against real downloaded chapters showed that ffmpeg's stream copy,
even with `-map_metadata 0`, does **not** preserve GoPro's proprietary
metadata. Comparing a chapter's `exiftool` output against the merged output
of that same chapter group:

- `CreateDate` / `ModifyDate` / `TrackCreateDate` / `MediaCreateDate` came
  back as `0000:00:00 00:00:00` instead of the real capture date.
- `Model`, `FirmwareVersion`, `CameraSerialNumber`, `LensSerialNumber`,
  `ColorMode`, `WhiteBalance`, `FieldOfView`, `ElectronicStabilizationOn`,
  and every other GoPro camera/lens/settings tag were missing entirely.

This happens because GoPro stores this information in a proprietary `udta`
atom that ffmpeg's generic metadata layer doesn't parse -- pointing
`-map_metadata` at a real chapter file (instead of the concat demuxer's
virtual input) recovers the date fields, but the proprietary tags remain
unreachable through ffmpeg no matter which input `-map_metadata` targets.

**Fix:** after the ffmpeg merge succeeds, GoSync runs `exiftool` to copy
every tag from the first chapter onto the merged file, since exiftool
(unlike ffmpeg) understands GoPro's proprietary atom structure:

```
exiftool -TagsFromFile <chapter 1> -all:all --Duration -overwrite_original -P <merged output>
```

- `--Duration` excludes the one metadata tag that must **not** be copied
  from chapter 1: `Duration` (the top-level `mvhd` movie duration) is
  writable, and blindly copying it would leave the merged file's declared
  duration equal to a single chapter's length instead of the true combined
  length. Every other structural field that scales with the merge
  (`FileSize`, `MediaDataSize`, `TrackDuration`, `MediaDuration`,
  `TimeScale`, `MediaTimeScale`) is already protected/non-writable in
  exiftool, so a blanket `-all:all` copy can't touch those regardless --
  `Duration` is the one field that needed an explicit exclusion.
- `-overwrite_original` avoids leaving a second full-size `_original` copy
  next to a multi-GB output file.
- `-P` preserves the merged file's filesystem modification time.
- Because the source chapters of one recording share identical camera/lens
  metadata (only the filename, filesystem timestamps, and per-chapter data
  size legitimately differ -- confirmed via `exiftool` across real chapter
  pairs), copying from chapter 1 alone is sufficient.
- If `exiftool` isn't installed, or the copy subprocess fails, the merge is
  still considered successful -- the video content is complete and correct
  either way, it just ends up without the extra camera/lens metadata. This
  is logged as a warning, not an error.

### Verifying a merged file's size

GoSync verifies a file is fully downloaded by comparing its on-disk size
against the `file_size` the GoPro API reported for that item. For a
chaptered recording, that reported size is exactly the sum of the raw
chapter files -- but the merged output ffmpeg/exiftool actually produce is
never byte-identical to that sum, because remuxing into a fresh MP4
container (new `moov`/`mdat` layout, copied metadata, `use_metadata_tags`)
changes the container overhead. Measured against real downloaded chapter
groups:

| chapters | API-reported size (sum of chapters) | actual merged size | difference |
|---|---|---|---|
| 2 | 6,337,761,802 bytes | 6,336,785,279 bytes | 976,523 bytes (0.0154%) |
| 2 | 6,765,676,261 bytes | 6,763,723,970 bytes | 1,952,291 bytes (0.0289%) |
| 7 | 74,418,086,168 bytes | 74,407,303,112 bytes | 10,783,056 bytes (0.0145%) |

The gap isn't a fixed percentage or a fixed amount per chapter -- it depends
on each file's specific stream structure -- so there is no formula to
predict it from chapter count alone. A naive exact-equality check between
on-disk size and the API's reported size therefore fails for every merged
recording, permanently marking a fully and correctly downloaded file as
incomplete and re-attempting the download on every resume-scan.

**Fix, in two parts:**

1. **Persist the real size.** Immediately after a batch finishes downloading
   (merge included), GoSync re-stats every file it just produced and
   overwrites the recorded `file_size` in its state with the actual on-disk
   size. From that point on, the recorded size reflects reality, not a
   stale API total that a completed merge could never match.
2. **Tolerant comparison.** The on-disk-size check no longer requires exact
   equality. It now requires:
   - `actual_size >= expected_size` -- the file may never be *smaller* than
     what was recorded; that still means truncation or corruption.
   - `actual_size <= expected_size * 1.01` -- growth is tolerated up to 1%,
     since re-running the merge tools isn't guaranteed to be perfectly
     byte-deterministic across tool versions.

   Anything outside that range reverts the item to pending with a
   `"File size mismatch"` error, same as before -- genuine truncated or
   corrupted downloads are still caught.

Non-chaptered items are unaffected in practice: a plain HTTP download's
on-disk size equals the API-reported size exactly, so it always satisfies
the tolerant check trivially.

### Merge configuration

| Variable | Default | Purpose |
|---|---|---|
| `FFMPEG_BINARY` | `ffmpeg` | Binary used to merge chapters. |
| `FFMPEG_TIMEOUT_SECONDS` | `900` | Timeout for the ffmpeg merge subprocess. |
| `EXIFTOOL_BINARY` | `exiftool` | Binary used to copy camera/lens metadata onto the merged output. |
| `EXIFTOOL_TIMEOUT_SECONDS` | `300` | Timeout for the exiftool metadata-copy subprocess. |

Both `ffmpeg` and `exiftool` are installed in the published Docker image.

## Sidecars And Chapters

XMP sidecars are generated from manifest metadata. A sidecar is written for the
manifest filename:

```text
downloads/<extension>/<manifest-filename>.xmp
```

When chapter files are successfully merged, there are no "extra" files left to
sidecar -- the single merged file receives the normal manifest-filename sidecar
like any other media item. Only in the fallback case (ffmpeg missing or the
merge failed) do separate, un-sidecared chapter files remain; GoSync does not
duplicate the parent manifest metadata across them.

See [XMP sidecar processing](sidecars.md) for the metadata allowlist and
excluded sensitive fields.
