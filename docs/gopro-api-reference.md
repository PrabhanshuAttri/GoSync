# GoPro Cloud API Reference (Reverse-Engineered)

Notes gathered while building the `test.py` / `test_gpx.py` / `test_telemetry.py`
/ `download_media.py` scripts in the repo root. These are undocumented
GoPro Plus endpoints discovered via the
[dustin/gopro-plus](https://github.com/dustin/gopro-plus) Haskell client and
verified live against a real account. GoSync itself does not call these
directly (it parses HAR exports instead — see
[media-items.md](media-items.md)); this doc is for the standalone
exploration scripts.

## Auth

All endpoints (except pre-signed CDN URLs) need these headers:

```text
Authorization: Bearer <AUTH_TOKEN>
Accept: application/vnd.gopro.jk.media+json; version=2.0.0
Content-Type: application/json
Accept-Language: en-US,en;q=0.9
User-Agent: github.com/dustin/gopro-plus 0.6.0.3
Origin: https://plus.gopro.com
Referer: https://plus.gopro.com/
```

`Origin`/`Referer` must be `plus.gopro.com`, not `gopro.com` — using the
wrong ones gets a `500 Internal Server Error` instead of a clean `401`.

`AUTH_TOKEN` is a JWE (5 dot-separated segments, `RSA-OAEP`/`A128GCM`
header) obtained by logging into gopro.com and capturing it from a
`api.gopro.com` request in DevTools. It expires (observed expiring within
a working session) and needs re-capturing when requests start failing with
`401 invalid_request`; there is no local way to check its expiry ahead of
time since the payload is encrypted, not just signed.

## Read Endpoints

| Endpoint | Returns |
|---|---|
| `GET /media/search?fields=...&page=&per_page=` | Paginated list, only the fields you request. Must paginate for full libraries (this account: 1662 items / 17 pages at `per_page=100`). `fields` accepts the **full** field set below — nothing is HAR-exclusive (see "No HAR-exclusive data" below). |
| `GET /media/{id}` | Full single-item record — richer than search, no `fields` param needed. Adds `available_labels`, `item_durations` (per-chapter, ms), `moments_count`, `content_title`, `content_description`, `music_track_artist`/`name`, `tags`, `folder_path`, `region`, `subscription_type`, `captured_at_timezone`, and sensitive fields `token`/`user_id`/`gopro_user_id` (see "Sensitive fields" below). |
| `GET /media/{id}/download` | `_embedded.files` (proxy/source renditions), `_embedded.variations` (quality renditions + signed URLs, `item_number` per chapter for video), `_embedded.sprites` (scrub thumbnails), `_embedded.sidecar_files` (see below). |
| `GET /media/{id}/moments?fields=time` | GoPro's auto-detected "Hero Moments" — verified live: `{"id":"...", "medium_id":"...", "frame":null, "time":9904294}` (ms into the recording). |
| `GET /media/filters/not-ready?page=&per_page=` | Account-wide items still processing (empty on a fully-processed account). |

### No HAR-exclusive data

Every field GoSync's HAR-derived XMP sidecars carry (`sidecar.py`'s
`VIDEO_SIDECAR_FIELDS`/`IMAGE_SIDECAR_FIELDS`) is available live via the
API — verified by requesting the full set on `/media/search` and diffing
against a real XMP file field-by-field: **zero fields missing**, values
identical. The `fields` param is just opt-in; nothing requires a captured
HAR:

```text
ai_training_opt_out,available_labels,camera_model,captured_at,captured_at_timezone,
created_at,file_extension,file_size,filename,firmware_version,height,moments_count,
orientation,play_as,ready_to_edit,ready_to_view,resolution,source_duration,
stabilized,thumbnail_available,type,updated_at,width
```

`GET /media/{id}` returns this whole set (and more) with no `fields` param
at all — it's the full record, not a projection.

### `available_labels`

Tells you what downloadable/derived assets exist for an item *without*
calling `/download` first — useful as a cheap pre-check (e.g. skip
attempting a `gpx` fetch when it's not in the list). Observed values:
`source`, `large`, `gpmf`, `gpx`, `mediainfo`, `high_res_proxy_mp4`,
`edit_proxy`, `audio_proxy`, `master_playlist`, `sprite`. This set **varies
per item** — one file had `gpx`+`mediainfo` but not `edit_proxy`/
`audio_proxy`/`sprite`; another had the reverse. Not a fixed schema.

| label | Meaning |
|---|---|
| `source` | Original file(s) as recorded, per-chapter for video. |
| `large` | Large-resolution JPEG rendition (poster frame / big preview), not the original. |
| `gpmf` | Raw GPMF telemetry track — see decoding section below. |
| `gpx` | Parsed GPS track derived from `gpmf`. |
| `mediainfo` | Technical source info — see `sidecar_files` table below. |
| `high_res_proxy_mp4` | Re-encoded lower-resolution proxy for playback/streaming. |
| `edit_proxy` | Lighter proxy tuned for editor timeline scrubbing. |
| `audio_proxy` | Audio extracted on its own (waveform/scrub without video). |
| `master_playlist` | HLS `.m3u8` manifest for adaptive-bitrate streaming. |
| `sprite` | Thumbnail sprite sheet for the scrubber preview. |

### Sensitive fields on `GET /media/{id}`

`token`, `user_id`, and `gopro_user_id` identify the account/access and
aren't useful "about the file" metadata — `download_media.py` strips all
three before writing anything to disk. `folder_path` (e.g.
`{account_uuid}/{media_id}/`) embeds that same UUID as its first path
segment; the script replaces the UUID with the literal placeholder
`{user_id}` so the path structure stays visible as a reference without
leaking the real identifier.

## `sidecar_files` labels (from `/media/{id}/download`)

One entry per chapter (`item_number`) for video, except `mediainfo` which
is account-wide once per media item:

| label | type | Contents |
|---|---|---|
| `gpx` | gpx | Parsed GPS track (lat/lon/ele/time/fix/pdop) — human-readable XML, ready to use. |
| `gpmf` | mp4 | Raw GoPro Metadata Format binary track — the source `gpx` is derived from. Needs manual decoding (see below). |
| `mediainfo` | json | Technical source info: codec, bitrate, fps, audio, and a `gopro` block (`lens`, `fov`, `eis`/`eis_active`, `retailName`, `generation`, `modelNumber`, `zoom`). Only one per media item, describing chapter 1 — camera settings don't vary across chapters of one recording. |

**Not every item has all three.** Older uploads may have `gpmf` only, with
no derived `gpx`/`mediainfo` — GoPro apparently didn't backfill those onto
pre-existing files (or the raw GPMF had no valid GPS fix to derive from).
Photos have `mediainfo` but never `gpx`/`gpmf`.

## Write / destructive endpoints (exist, not exercised by these scripts)

| Endpoint | Effect |
|---|---|
| `GET /media?ids={id}` | Used as delete. |
| `POST /media/{id}/process` | Forces GoPro to regenerate derived assets — could backfill missing `gpx`/`mediainfo` on old uploads, but mutates account state. |
| `PUT /media/{id}` | Edit title/description/tags. |

## Dead end

`https://images-0[1-4].gopro.com/resize/450wwp/{token}` (thumbnail via the
per-item `token` from `GET /media/{id}`) returned `404` on all four shards
when tested — looks deprecated. Use the `sprites`/`variations` signed URLs
from `/download` instead; those are confirmed working.

No user/account profile endpoint (`/users/me` or similar) exists in the
reference client.

## Decoding raw GPMF (`gpmf` sidecar)

The `gpmf` sidecar is an mp4 container with a `gpmd`-tagged data track in
GoPro's own binary KLV (key-length-value) format
([spec](https://github.com/gopro/gpmf-parser)). Two dead ends before
finding a working recipe:

- `telemetry-parser` (PyPI) — the real Rust-based decoder (Gyroflow
  project), but needs a Rust toolchain to build; not available in this
  environment.
- `gpmf` (PyPI, aka `pygpmf`) — installs, but its `io.extract_gpmf_stream()`
  is broken: it expects the `ffmpeg-python` package's `.probe()` API while
  its actual declared dependency, `python-ffmpeg`, is a different,
  incompatible package that happens to share the `ffmpeg` import name.

**Working recipe** — extract the track with plain `ffmpeg`, then use only
`gpmf.parse` (which doesn't touch the broken `io` module):

```bash
ffmpeg -i chapter.mp4 -map 0:d:0 -c copy -f data raw_gpmf.bin
```

```python
import gpmf.parse as gp
expanded = gp.expand_klv(open("raw_gpmf.bin", "rb").read())  # list of ~1/sec DEVC blocks
```

Most streams (`ACCL`, `GYRO`, `CORI`, `GRAV`) are plain numeric arrays the
library decodes correctly on its own — just divide by the sibling `SCAL`
key to get real units. **Watch out**: `SCAL` can be a single scalar
(applies to every axis, e.g. `ACCL`) or an array (one divisor per field,
e.g. `GPS9`) — code must handle both.

`GPS9` is a GPMF "complex type" (`type='?'` in its KLV header) and the
library can't decode it generically. It has to be unpacked manually using
its sibling `TYPE` key (a per-field format string, e.g. `"lllllllSS"` = 7×
int32 + 2× uint16) and `SCAL`:

```python
import struct
from datetime import datetime, timedelta

FIELDS = ["lat", "lon", "alt", "speed_2d", "speed_3d", "days", "secs", "dop", "fix"]
fmt = ">lllllllHH"
for i in range(repeat):
    raw = struct.unpack(fmt, gps9_bytes[i*32:(i+1)*32])
    values = [v / s for v, s in zip(raw, scal)]
    row = dict(zip(FIELDS, values))
    row["time"] = (datetime(2000, 1, 1) + timedelta(days=row["days"], seconds=row["secs"])).isoformat()
```

Verified against known-good `.gpx` coordinates from the same chapter —
decoded lat/lon matched exactly.

### GPMF stream keys observed in one HERO11 Black chapter

`GPS9`, `GPS5`, `GPSF`, `GPSP`, `GPSU`, `ACCL`, `GYRO`, `CORI`, `GRAV`,
`ISOE`, `SHUT`, `WBAL`, `WRGB`, `TMPC`, `YAVG`, `SCEN`, `HUES`, `FACE`
(per-frame face-detection bounding boxes — privacy-sensitive), plus
structural keys (`DVID`, `DVNM`, `STRM`, `STNM`, `TYPE`, `SCAL`, `UNIT`,
`SIUN`, `STMP`, `TSMP`).

## Known quirks

- The very first samples of a recording before GPS lock read
  `lat=0, lon=0, fix=0, dop=99.99` with a stale/wrong timestamp (e.g.
  `2021-03-13` on a clip actually shot in 2026) — the camera's internal
  clock default before it gets a real GPS time fix. Not a decoding bug.
- **A `<fix>2d</fix>` point's altitude is not trustworthy, even though its
  lat/lon are fine.** A 2D fix only solves for latitude/longitude; altitude
  is unconstrained and swings wildly — verified: consecutive 2D-fix points
  at an effectively stationary spot jumped between `-6.28m` and `+4.02m`
  within single-second steps. Only a `<fix>3d</fix>` point solves altitude
  too, and its elevation drifts smoothly (e.g. `-16.8m` → `-17.2m` over 30
  seconds) rather than jumping. `download_media.py`'s `first_gps_fix()`
  requires `3d` specifically for this reason. Even then, treat altitude
  precision skeptically when `pdop` is high (a `pdop=10.6` 3D fix is still
  low-confidence — GPS altitude error is inherently 2-3x worse than
  horizontal error, and water/boat locations get worse satellite geometry
  from multipath).
- `test.py`'s filename search only scans the first page of `/media/search`
  (`per_page` items) — files further back in the library (e.g. older
  chapters) won't be found unless the search paginates through all pages,
  as `test_gpx.py`/`test_telemetry.py`/`download_media.py` already do.

## Companion scripts (repo root)

- `test.py` — list/search media by filename (single page).
- `test_gpx.py` — filename/ID → merged `.gpx` (all chapters, one
  `<trkseg>` per chapter) + `mediainfo.json`, into `gpx_downloads/<name>/`.
- `test_telemetry.py` — filename/ID → decoded CSV per requested GPMF stream
  (`GPS9` default; also `ACCL`, `GYRO`, `CORI`, `GRAV`), merged across
  chapters, into the same `gpx_downloads/<name>/` folder.
- `download_media.py` — the combined, interactive tool:
  - Lists the **whole account** (paginated) and caches it to
    `gpx_downloads/media_list.json`, offering to reuse the cache next run.
  - Prompts for a filename (substring match, numbered picker on multiple
    hits) or a **comma-separated list of exact filenames** to batch-select
    several items at once — matched by filename stem, so the extension is
    optional (`GX010428` matches `GX010428.MP4`).
  - Downloads the original media (`source`-quality `variations`, per
    chapter) behind a confirmation prompt showing the **real total size**
    pulled from the cached listing (chaptered recordings here run into
    tens of GB — `variations` entries themselves carry no `file_size`).
  - Downloads GPX (merged across chapters), falling back to raw `.gpmf`
    per chapter when no derived `gpx` exists.
  - Writes a combined `<name>_mediainfo.json`: the full `GET /media/{id}`
    record (sensitive fields stripped, `folder_path` UUID redacted) plus
    the `mediainfo` sidecar (or `null` if GoPro never generated one) —
    plus a `geoData`/`geoDataExif` block (Google Photos Takeout format)
    derived from the first 3D-fix GPX point, when GPX is available.
  - `--dry-run` flag: fetch all sidecars, skip the (potentially huge)
    original media file entirely.
