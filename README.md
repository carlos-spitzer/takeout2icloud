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
```

## Commands

| Command | Description |
|---------|-------------|
| `scan <path>` | Walk Takeout directory, parse sidecars, build SQLite DB |
| `curate` | Deduplicate album vs year-bucket copies |
| `stage` | Copy pending files to `~/.takeout2icloud/staging/` |
| `fix-exif` | Write JSON sidecar metadata into file EXIF tags |
| `import` | Import into Photos.app with album assignments |
| `run <path>` | Run all five phases in sequence (resumable) |
| `retry` | Reset failed files so they can be reprocessed |
| `status` | Show current progress |

## Import flags

- `--limit N`: process only N files (for testing)
- `--dry-run`: preview what would be imported
- `--album <name>`: only import files belonging to this album

## Pipeline phases

### 1. Scan

Walks the Takeout tree, matches each media file to its `.json` sidecar
(both new `.supplemental-metadata.json` and legacy formats), computes
SHA256 hashes, and stores everything in `~/.takeout2icloud/state.db`.

### 2. Curate

Marks year-bucket files as duplicates when the same SHA256 exists in a
named album folder. Unique unalbumized photos are kept.

### 3. Stage

Copies files to a flat staging directory (`~/.takeout2icloud/staging/`).
Handles filename collisions. Originals are never modified.

### 4. Fix EXIF

Uses `exiftool` to write authoritative timestamps, GPS coordinates,
titles, and descriptions from the JSON sidecars into the staged files.
Also detects and fixes mismatched file extensions (e.g. JPEG saved as
`.HEIC` by Google).

### 5. Import

Uses `photoscript` (from `osxphotos`) to drive Photos.app via Apple
Events. Creates albums and assigns photos to them.

**Dialog dismisser**: Photos.app sometimes shows error dialogs
("No se pueden importar N items") that block the Apple Events bridge.
A companion bash script (`dismiss_dialogs.sh`) runs as a **separate OS
process** to auto-dismiss these dialogs, avoiding the Apple Events
deadlock that occurs with background threads.

**Multi-pass strategy**: When Photos.app shows an error dialog mid-batch,
`import_photos()` returns an empty list. Rather than marking those files
as failed, this tool leaves them as `exif_fixed` so a subsequent run
retries them. Only files that are explicitly unmatched in a
partially-successful batch get marked as genuinely rejected.

The `DISMISS_DELAY` environment variable (default: 1 second) controls how
long the dismisser waits after detecting a dialog before closing it,
giving `import_photos()` time to read the result.

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

## State and logs

| Path | Purpose |
|------|---------|
| `~/.takeout2icloud/state.db` | SQLite database tracking all file states |
| `~/.takeout2icloud/staging/` | Flat directory with staged copies |
| `~/.takeout2icloud/errors.log` | Error log from all phases |
| `~/.takeout2icloud/dismiss.log` | Log of auto-dismissed Photos.app dialogs |

The process is fully resumable. Re-running any command skips files that
have already progressed past that phase.

## Important

iCloud will begin syncing immediately after import starts. Make sure you
have sufficient iCloud storage before running a full import.

## License

MIT
