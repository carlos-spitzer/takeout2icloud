import hashlib
import json
import logging
import os
import re
from pathlib import Path

from rich.console import Console
from rich.table import Table

from models import MediaItem, AlbumMembership, ScanSummary
from state import (
    get_connection, upsert_media, add_album_membership, reset_scan,
    get_known_paths, mark_phase_complete,
)

logger = logging.getLogger(__name__)
console = Console()

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".gif", ".webp",
}

YEAR_BUCKET_PATTERN = re.compile(r"^(?:Photos from|Fotos del) \d{4}$")


def sha256_file(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def classify_folder(folder_name: str) -> tuple[str, str | None]:
    if YEAR_BUCKET_PATTERN.match(folder_name):
        return "year_bucket", None
    return "album", folder_name


def find_sidecar(media_path: Path) -> Path | None:
    parent = media_path.parent
    name = media_path.name
    stem = media_path.stem
    ext = media_path.suffix

    # --- New Takeout format (2024+): .supplemental-metadata.json ---
    # Google appends ".supplemental-metadata.json" but truncates the full
    # sidecar filename at ~51 chars when the media name is long.
    supp_suffix = ".supplemental-metadata.json"
    full_supp = name + supp_suffix  # e.g. "photo.jpg.supplemental-metadata.json"
    if len(full_supp) <= 51:
        candidate = parent / full_supp
        if candidate.exists():
            return candidate
    else:
        # Truncated: keep first (51 - len(".json")) = 46 chars + ".json"
        truncated = full_supp[:46] + ".json"
        candidate = parent / truncated
        if candidate.exists():
            return candidate

    # Also try glob for unpredictable truncation lengths
    # e.g. "photo.jpg.supplemental-metadata.json", "photo.jpg.supp.json"
    pattern = f"{name}.supp*json"
    matches = list(parent.glob(pattern))
    if matches:
        return matches[0]

    # --- Legacy Takeout format: .json suffix ---
    # 1. Exact match: photo.jpg -> photo.jpg.json
    candidate = parent / f"{name}.json"
    if candidate.exists():
        return candidate

    # 1b. Double-dot variant: photo.jpg..json
    candidate = parent / f"{name}..json"
    if candidate.exists():
        return candidate

    # 2. Truncated name: base[:46] + ext + .json
    if len(stem) > 46:
        truncated = stem[:46] + ext + ".json"
        candidate = parent / truncated
        if candidate.exists():
            return candidate

    # 3. Edited suffix: strip -edited
    if stem.endswith("-edited"):
        original_stem = stem[: -len("-edited")]
        candidate = parent / f"{original_stem}{ext}.json"
        if candidate.exists():
            return candidate

    return None


def parse_sidecar(sidecar_path: Path) -> dict:
    with open(sidecar_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result: dict = {}

    taken_time = data.get("photoTakenTime", {})
    ts = taken_time.get("timestamp")
    if ts:
        result["capture_ts"] = int(ts)

    geo_exif = data.get("geoDataExif", {})
    geo_data = data.get("geoData", {})

    lat = geo_exif.get("latitude", 0.0)
    lon = geo_exif.get("longitude", 0.0)
    alt = geo_exif.get("altitude", 0.0)

    if lat == 0.0 and lon == 0.0:
        lat = geo_data.get("latitude", 0.0)
        lon = geo_data.get("longitude", 0.0)
        alt = geo_data.get("altitude", 0.0)

    if not (lat == 0.0 and lon == 0.0):
        result["latitude"] = lat
        result["longitude"] = lon
        result["altitude"] = alt

    title = data.get("title", "")
    if title:
        result["title"] = title

    description = data.get("description", "")
    if description:
        result["description"] = description

    return result


def scan(takeout_path: str, rescan: bool = False) -> ScanSummary:
    root = Path(takeout_path).resolve()
    if not root.is_dir():
        console.print(f"[red]Error: {takeout_path} is not a directory[/red]")
        raise SystemExit(1)

    conn = get_connection()

    if rescan:
        reset_scan(conn)
        known_paths: set[str] = set()
        console.print("[yellow]Full rescan requested, cleared previous data.[/yellow]")
    else:
        known_paths = get_known_paths(conn)
        if known_paths:
            console.print(f"[dim]Incremental scan: {len(known_paths)} files already in DB, skipping those.[/dim]")

    summary = ScanSummary()
    skipped = 0
    seen_folders: set[str] = set()

    from rich.progress import Progress
    with Progress(console=console) as progress:
        task = progress.add_task("Scanning...", total=None)

        for dirpath, _dirnames, filenames in os.walk(root):
            dir_p = Path(dirpath)
            folder_name = dir_p.name

            for fname in filenames:
                fpath = dir_p / fname
                ext = fpath.suffix.lower()
                if ext not in MEDIA_EXTENSIONS:
                    continue

                summary.total_files += 1
                progress.update(task, description=f"Scanning... {summary.total_files} files ({skipped} skipped)")

                if str(fpath) in known_paths:
                    skipped += 1
                    folder_type, album_name = classify_folder(folder_name)
                    if folder_name not in seen_folders:
                        seen_folders.add(folder_name)
                        if folder_type == "album":
                            summary.album_folders.append(folder_name)
                        else:
                            summary.year_bucket_folders.append(folder_name)
                    if folder_type == "album":
                        summary.album_files += 1
                    else:
                        summary.year_bucket_files += 1
                    continue

                folder_type, album_name = classify_folder(folder_name)

                if folder_name not in seen_folders:
                    seen_folders.add(folder_name)
                    if folder_type == "album":
                        summary.album_folders.append(folder_name)
                    else:
                        summary.year_bucket_folders.append(folder_name)

                if folder_type == "album":
                    summary.album_files += 1
                else:
                    summary.year_bucket_files += 1

                file_hash = sha256_file(fpath)

                sidecar = find_sidecar(fpath)
                metadata: dict = {}
                if sidecar:
                    try:
                        metadata = parse_sidecar(sidecar)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning("Failed to parse sidecar %s: %s", sidecar, e)
                        summary.missing_sidecars += 1
                else:
                    summary.missing_sidecars += 1
                    metadata["capture_ts"] = int(fpath.stat().st_mtime)

                item = MediaItem(
                    sha256=file_hash,
                    source_path=str(fpath),
                    canonical_path=str(fpath),
                    filename=fname,
                    folder_type=folder_type,
                    album_name=album_name,
                    capture_ts=metadata.get("capture_ts"),
                    latitude=metadata.get("latitude"),
                    longitude=metadata.get("longitude"),
                    altitude=metadata.get("altitude"),
                    title=metadata.get("title"),
                    description=metadata.get("description"),
                )

                upsert_media(conn, item)

                if album_name:
                    add_album_membership(conn, AlbumMembership(
                        sha256=file_hash, album_name=album_name,
                    ))

                if (summary.total_files - skipped) % 500 == 0:
                    conn.commit()

    conn.commit()
    mark_phase_complete(conn, "scan", summary.total_files)
    conn.close()

    _print_scan_summary(summary, skipped)
    return summary


def _print_scan_summary(summary: ScanSummary, skipped: int = 0) -> None:
    console.print()
    console.print("[bold green]Scan complete[/bold green]")

    table = Table(title="Scan Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white", justify="right")

    table.add_row("Total media files", str(summary.total_files))
    if skipped:
        table.add_row("Already in DB (skipped)", str(skipped))
        table.add_row("New files added", str(summary.total_files - skipped))
    table.add_row("Album folders", str(len(summary.album_folders)))
    table.add_row("Year bucket folders", str(len(summary.year_bucket_folders)))
    table.add_row("Files in albums", str(summary.album_files))
    table.add_row("Files in year buckets", str(summary.year_bucket_files))
    table.add_row("Missing sidecars", str(summary.missing_sidecars))

    console.print(table)

    if summary.album_folders:
        console.print("\n[bold]Album folders:[/bold]")
        for name in sorted(summary.album_folders):
            console.print(f"  - {name}")

    if summary.year_bucket_folders:
        console.print(f"\n[bold]Year bucket folders:[/bold] {', '.join(sorted(summary.year_bucket_folders))}")
