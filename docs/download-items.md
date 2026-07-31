# Download Items

This document explains how GoSync turns GoPro Cloud HAR data into local
download records, how those records move through state and extraction, and how
GoPro chapter files are merged into a single file.

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
extraction.

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
where needed.

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

## Chapter Files

Some GoPro recordings are split into multiple physical files called chapters.
The HAR may describe the recording as one manifest item, while the zip download
contains more than one media file for that recording (e.g. `GX010320.MP4`,
`GX020320.MP4`, `GX030320.MP4` for a 3-chapter recording).

GoPro chapter filenames encode play order in the filename itself
(`G[HX]<chapter><group>.<ext>`, chapter ascending; legacy `GOPR<group>.<ext>`
is chapter 1, followed by `G[PH]<chapter><group>.<ext>`). Embedded MP4/QuickTime
metadata cannot be used for ordering or to tell chapters apart: GoPro stamps
every chapter of a recording with identical session-level `Create Date`,
`Duration`, camera/firmware/lens fields, and `Media UID` prefix. The only
per-chapter differences are the filename and each chapter's actual encoded
payload size.

After extraction, GoSync:

- Finds the sibling chapter files for the manifest item by matching filename
  extension and the parsed GoPro group ID, sorted ascending by chapter number.
- Concatenates them via `ffmpeg`'s concat demuxer with stream copy (`-c copy`,
  no re-encoding) into a single file at the manifest item's expected target
  path, mapping all streams (`-map 0`) so GoPro's `gpmd` telemetry and `tmcd`
  timecode tracks are preserved, and carrying forward global metadata
  (`-map_metadata 0 -movflags use_metadata_tags`) so camera model, firmware,
  and lens fields survive onto the merged file.
- Moves the original chapter files into
  `downloads/original_unmerged_<extension>/` once the merge succeeds, instead
  of deleting them, so the raw per-chapter footage is still recoverable.
- Marks the manifest item downloaded like any other single-file item.

While the merge is running, GoSync reports a `Merging` status (CLI log line
and web dashboard) so long-running `ffmpeg` merges are visible instead of
appearing to hang.

If `ffmpeg` is not installed, or the merge subprocess fails, GoSync falls back
to the previous behavior: the manifest filename is moved into the expected
extension folder, and the remaining chapter files are moved alongside it as
separate files rather than being lost. A warning or error event is logged in
this case so the fallback is visible.

- Extra chapter files may not appear as separate rows in `manifest.json`,
  `gosync_state.json`, or the web UI.
- The parent manifest item keeps the download status and retry state.

This preserves all physical files from the zip without inventing metadata that
was not present as a separate HAR media record.

## Sidecars And Chapters

XMP sidecars are generated from manifest metadata. A sidecar is written for the
manifest filename:

```text
downloads/<extension>/<manifest-filename>.xmp
```

When chapter files are successfully merged, there are no "extra" files left to
sidecar — the single merged file receives the normal manifest-filename sidecar
like any other media item. Only in the fallback case (ffmpeg missing or the
merge failed) do separate, un-sidecared chapter files remain; GoSync does not
duplicate the parent manifest metadata across them.

See [XMP sidecar processing](sidecars.md) for the metadata allowlist and
excluded sensitive fields.
