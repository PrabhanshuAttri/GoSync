# XMP Sidecar Processing

GoSync generates XMP sidecars from metadata already present in the exported HAR
file. It does not call another metadata API for sidecars.

## Flow

When a web download job starts, GoSync parses the HAR into a shared manifest and
starts two background threads:

- the media download thread, which downloads and extracts media files
- the sidecar thread, which uses the manifest metadata and writes XMP files

The sidecar thread runs independently. Its status is shown as a secondary XMP
line in Current Activity and in the Event Log. It does not stop or block media
downloads.

## HAR Source

Sidecar processing scans `log.entries[]` in the HAR and uses entries whose
`request.url` contains:

```text
https://api.gopro.com/media/search
```

For each matching entry, it reads:

```text
response.content.text
```

That text is parsed as JSON. Media metadata is usually found under
`_embedded.media`, though the parser also handles common list keys and nested
media objects.

## Output

Sidecars are written next to their media files in lowercase extension folders
inside the configured download folder:

```text
data/downloads/mp4/example-video.MP4
data/downloads/mp4/example-video.MP4.xmp
```

The default name format is:

```text
<filename>.<extension>.xmp
```

If the filename already contains the extension, GoSync does not duplicate it.

## Field Selection

Sidecars use a curated allowlist instead of writing every field returned by
GoPro. This keeps credentials, account identifiers, and internal cloud IDs out
of the XMP files.

Common image and video fields:

```text
ai_training_opt_out
camera_model
captured_at
captured_at_timezone
content_title
content_type
created_at
file_extension
file_size
filename
firmware_version
fov
height
orientation
play_as
ready_to_view
resolution
submitted_at
thumbnail_available
type
updated_at
width
```

Additional video fields:

```text
available_labels
mce_type
moments_count
ready_to_edit
source_duration
stabilized
```

## Excluded Fields

These fields are intentionally not written into XMP sidecars:

```text
token
gopro_user_id
id
source_gumi
source_mgumi
export_ids
on_public_profile
```

Pagination and response wrapper fields are also excluded, such as:

```text
_embedded
_pages
current_page
errors
item_count
media
per_page
total_items
total_pages
```
