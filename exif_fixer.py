import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress

from state import get_connection, get_media_by_status, update_status, update_canonical_path

# Map of `file` command output keywords to correct extensions
_MAGIC_TO_EXT = {
    "JPEG image": ".jpg",
    "PNG image": ".png",
    "GIF image": ".gif",
    "HEIF": ".heic",
    "ISO Media": ".mp4",
    "WebP": ".webp",
}

logger = logging.getLogger(__name__)
console = Console()


def _detect_real_ext(filepath: Path) -> str | None:
    """Use `file` command to detect the true file type and return the correct extension."""
    try:
        result = subprocess.run(
            ["file", "--brief", str(filepath)],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.strip()
        for magic, ext in _MAGIC_TO_EXT.items():
            if magic in output:
                return ext
    except Exception:
        pass
    return None


def _fix_extension_if_needed(filepath: Path, sha256: str, source_path: str,
                              conn) -> Path:
    """Rename the staged file if its extension doesn't match its actual content."""
    real_ext = _detect_real_ext(filepath)
    if real_ext is None:
        return filepath

    current_ext = filepath.suffix.lower()
    if current_ext == real_ext:
        return filepath

    new_path = filepath.with_suffix(real_ext)
    # Avoid collision
    if new_path.exists():
        new_path = filepath.parent / f"{filepath.stem}_fixed{real_ext}"

    filepath.rename(new_path)
    update_canonical_path(conn, sha256, source_path, str(new_path))
    logger.info("Renamed %s -> %s (was %s, actually %s)", filepath.name, new_path.name, current_ext, real_ext)
    return new_path


def check_exiftool() -> None:
    if not shutil.which("exiftool"):
        console.print("[red]exiftool not found.[/red]")
        console.print("Install it with: [bold]brew install exiftool[/bold]")
        raise SystemExit(1)


def _build_exiftool_args(row: dict) -> list[str]:
    # -m: ignore minor errors and warnings (e.g. corrupted IFD pointers from
    # Google's processing)
    args = ["exiftool", "-overwrite_original", "-m"]

    if row.get("capture_ts"):
        dt = datetime.fromtimestamp(row["capture_ts"], tz=timezone.utc).astimezone()
        ts_str = dt.strftime("%Y:%m:%d %H:%M:%S")
        args.extend([
            f"-DateTimeOriginal={ts_str}",
            f"-CreateDate={ts_str}",
            f"-ModifyDate={ts_str}",
        ])

    lat = row.get("latitude")
    lon = row.get("longitude")
    alt = row.get("altitude")

    if lat is not None and lon is not None and not (lat == 0.0 and lon == 0.0):
        lat_ref = "N" if lat >= 0 else "S"
        lon_ref = "E" if lon >= 0 else "W"
        args.extend([
            f"-GPSLatitude={abs(lat)}",
            f"-GPSLatitudeRef={lat_ref}",
            f"-GPSLongitude={abs(lon)}",
            f"-GPSLongitudeRef={lon_ref}",
        ])
        if alt is not None:
            args.extend([
                f"-GPSAltitude={abs(alt)}",
                f"-GPSAltitudeRef={'Above Sea Level' if alt >= 0 else 'Below Sea Level'}",
            ])

    title = row.get("title")
    filename_stem = Path(row["filename"]).stem
    if title and title != row["filename"] and title != filename_stem:
        args.append(f"-XPTitle={title}")

    description = row.get("description")
    if description:
        args.append(f"-ImageDescription={description}")

    args.append(row["canonical_path"])
    return args


def fix_exif(limit: Optional[int] = None) -> int:
    check_exiftool()

    conn = get_connection()
    staged = get_media_by_status(conn, "staged", limit=limit)

    if not staged:
        console.print("[yellow]No staged files to fix.[/yellow]")
        conn.close()
        return 0

    fixed_count = 0

    with Progress(console=console) as progress:
        task = progress.add_task("Fixing EXIF metadata...", total=len(staged))

        for row in staged:
            filepath = Path(row["canonical_path"])
            if not filepath.exists():
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message="Staged file not found")
                progress.advance(task)
                continue

            # Fix mismatched extensions (Google Takeout frequently saves
            # JPEGs as .HEIC, PNGs as .jpg, etc.)
            filepath = _fix_extension_if_needed(filepath, row["sha256"], row["source_path"], conn)
            row["canonical_path"] = str(filepath)

            args = _build_exiftool_args(row)

            try:
                result = subprocess.run(
                    args, capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    error_msg = result.stderr.strip() or result.stdout.strip()
                    update_status(conn, row["sha256"], row["source_path"],
                                  "failed", error_message=f"exiftool error: {error_msg}")
                    logger.error("exiftool failed for %s: %s", filepath, error_msg)
                else:
                    update_status(conn, row["sha256"], row["source_path"], "exif_fixed")
                    fixed_count += 1
            except subprocess.TimeoutExpired:
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message="exiftool timeout")
            except OSError as e:
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message=str(e))

            progress.advance(task)

            if fixed_count % 100 == 0:
                conn.commit()

    conn.commit()
    conn.close()

    console.print(f"[bold green]Fixed EXIF on {fixed_count} files[/bold green]")
    return fixed_count
