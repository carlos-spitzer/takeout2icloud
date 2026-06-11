# takeout2icloud

Import a Google Takeout Photos export into macOS Photos.app (iCloud Photos).

Reads Google's JSON sidecar metadata (timestamps, GPS, descriptions), writes it
into EXIF tags, deduplicates album vs year-bucket copies, and imports into
Photos.app with album assignments preserved.

## Prerequisites

- **Python 3.11+**
- **exiftool**: `brew install exiftool`
- **macOS** with Photos.app and iCloud Photos enabled
- **Photos.app** must be open during the import phase

## Installation

```bash
cd takeout2icloud
pip install -e .
```

## How to export from Google Takeout

1. Go to [takeout.google.com](https://takeout.google.com)
2. Deselect all, then select only **Google Photos**
3. Choose **All photo albums included** (default)
4. Export format: **.tgz**, max file size: **10 GB**
5. Download all archive parts and extract them into a single folder

The extracted folder will typically be at:
`Takeout/Google Fotos/` (or `Takeout/Google Photos/` depending on locale).

## Quick start

```bash
# Full pipeline, resumable
takeout2icloud run /path/to/Takeout/Google\ Fotos

# Or step by step:
takeout2icloud scan /path/to/Takeout/Google\ Fotos
takeout2icloud curate
takeout2icloud stage
takeout2icloud fix-exif
takeout2icloud import

# After import, verify and sync album assignments
takeout2icloud sync-albums --dry-run   # audit only
takeout2icloud sync-albums             # apply corrections
```

## Commands

| Command | Description |
|---------|-------------|
| `scan <path>` | Walk Takeout directory, parse sidecars, build SQLite DB |
| `curate` | Deduplicate album vs year-bucket copies |
| `stage` | Copy pending files to `~/.takeout2icloud/staging/` with album-prefixed filenames |
| `fix-exif` | Write JSON sidecar metadata into file EXIF tags |
| `import` | Import into Photos.app one-by-one via osascript with duplicate prevention |
| `run <path>` | Run all five phases in sequence (resumable) |
| `retry` | Reset failed files so they can be reprocessed |
| `repair-uuids` | Fix invalid photo UUIDs in the state DB by cross-referencing Photos.app |
| `sync-albums` | Verify and correct iCloud album memberships against Google Takeout |
| `status` | Show current progress |

## Import flags

- `--limit N`: process only N files (for testing)
- `--dry-run`: preview what would be imported
- `--album <name>`: only import files belonging to this album

## Pipeline phases

### 1. Scan

Walks the Takeout tree, matches each media file to its `.json` sidecar
(both new `.supplemental-metadata.json` and legacy formats, including
double-dot variants), computes SHA256 hashes, and stores everything in
`~/.takeout2icloud/state.db`.

### 2. Curate

Marks year-bucket files as duplicates when the same SHA256 exists in a
named album folder. Unique unalbumized photos are kept.

### 3. Stage

Copies files to a flat staging directory (`~/.takeout2icloud/staging/`).
Filenames are prefixed with the album name (`AlbumName__photo.jpg`) to
prevent cross-album collisions. Originals are never modified.

### 4. Fix EXIF

Uses `exiftool` to write authoritative timestamps, GPS coordinates,
titles, and descriptions from the JSON sidecars into the staged files.
Also detects and fixes mismatched file extensions (e.g. JPEG saved as
`.HEIC` by Google).

### 5. Import

Uses direct `osascript` (AppleScript) calls to drive Photos.app. Imports
one file at a time with:

- **Duplicate prevention**: builds an in-memory filename index from
  Photos.app before importing; skips files that already exist
- **Ghost import detection**: when osascript returns an error but
  Photos.app silently imported the file, detects it via filename search
- **Auto-restart**: after 20 consecutive failures, quits and relaunches
  Photos.app to recover from its periodic broken state
- **Album assignment**: creates albums on demand and assigns each photo
  to its correct album(s)

### 6. Sync albums (post-import)

Compares expected album memberships (from the state DB) against actual
albums in Photos.app, then adds missing photos and removes extras.
Includes a UUID repair step for photos whose DB UUID doesn't match
Photos.app.

## Known edge cases

- **Truncated filenames**: Google truncates base filenames at 46
  characters. The scanner tries both full and truncated names when
  matching sidecars.
- **Edited files**: files named `photo-edited.jpg` are matched to
  `photo.jpg.json` sidecars.
- **Missing sidecars**: files without a JSON sidecar use the file's
  modification date as a fallback timestamp.
- **Live photo pairs**: `.mov` companions to `.heic` files are imported
  separately; Photos.app may or may not re-pair them.
- **Extension mismatches**: Google sometimes saves JPEGs as `.HEIC` or
  PNGs as `.jpg`. The fix-exif phase detects and renames these using the
  `file` command.
- **Photos.app crashes**: after ~2,500 consecutive imports, Photos.app
  enters a broken state. The importer auto-restarts it and continues.
- **Unicode normalization**: album names may differ between NFD and NFC
  forms; the sync module handles both.

## State and logs

| Path | Purpose |
|------|---------|
| `~/.takeout2icloud/state.db` | SQLite database tracking all file states |
| `~/.takeout2icloud/staging/` | Flat directory with staged copies (can be deleted after import) |
| `~/.takeout2icloud/errors.log` | Error log from all phases |

The process is fully resumable. Re-running any command skips files that
have already progressed past that phase.

## Important

iCloud will begin syncing immediately after import starts. Make sure you
have sufficient iCloud storage before running a full import.

## License

MIT
