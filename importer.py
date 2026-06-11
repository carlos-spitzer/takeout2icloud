"""Phase 5: Import EXIF-fixed files into macOS Photos.app.

Uses AppleScript (osascript) directly to drive Photos.app.
Imports one file at a time with timeout-based dialog dismissal.
Pre-checks for duplicates using an in-memory filename index.
Fully unattended.
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress

from state import get_connection, get_media_by_status, get_album_memberships, update_status

logger = logging.getLogger(__name__)
console = Console()

IMPORT_TIMEOUT = 20
COMMIT_INTERVAL = 50


def check_photos_running() -> bool:
    result = subprocess.run(["pgrep", "-x", "Photos"], capture_output=True)
    return result.returncode == 0


def _validate_file(filepath: Path) -> bool:
    return filepath.exists() and filepath.stat().st_size > 0


def _dismiss_dialog() -> bool:
    script = '''
    tell application "System Events"
        tell process "Photos"
            if exists sheet 1 of window 1 then
                try
                    set btns to name of every button of sheet 1 of window 1
                    if btns contains "Aceptar" then
                        click button "Aceptar" of sheet 1 of window 1
                    else if btns contains "No importar" then
                        click button "No importar" of sheet 1 of window 1
                    else if btns contains "OK" then
                        click button "OK" of sheet 1 of window 1
                    else
                        keystroke return
                    end if
                    return "dismissed"
                on error
                    return "error"
                end try
            end if
        end tell
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    return "dismissed" in result.stdout


def _build_photos_filename_index() -> dict[str, str]:
    """Build filename -> UUID map from Photos.app for duplicate detection."""
    console.print("[dim]Building Photos.app filename index for duplicate detection...[/dim]")
    script = '''
    tell application "Photos"
        set ids to id of every media item
        set fns to filename of every media item
        set output to ""
        repeat with i from 1 to count of ids
            set output to output & (item i of ids) & "|" & (item i of fns) & linefeed
        end repeat
        return output
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=300,
    )
    fn_to_uuid: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if "|" not in line:
            continue
        raw_id, fn = line.split("|", 1)
        uuid = raw_id.split("/")[0] if "/" in raw_id else raw_id
        fn_to_uuid[fn] = uuid
    console.print(f"[dim]Indexed {len(fn_to_uuid)} items in Photos.app[/dim]")
    return fn_to_uuid


def _osascript_import(filepath: str) -> Optional[str]:
    escaped = filepath.replace('"', '\\"')
    script = f'''
    tell application "Photos"
        set theFile to POSIX file "{escaped}"
        set imported to import {{theFile}} skip check duplicates yes
        if (count of imported) > 0 then
            return id of item 1 of imported
        else
            return "EMPTY"
        end if
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=IMPORT_TIMEOUT,
        )
        stdout = result.stdout.strip()
        if result.returncode == 0 and stdout and stdout != "EMPTY":
            uuid = stdout.split("/")[0] if "/" in stdout else stdout
            return uuid
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Import timed out for %s", filepath)
        _dismiss_dialog()
        time.sleep(1)
        _dismiss_dialog()
        return None
    except Exception as e:
        logger.error("Import error for %s: %s", filepath, e)
        return None


def _assign_albums_osascript(uuid: str, sha256: str, conn, album_cache: dict) -> None:
    albums = get_album_memberships(conn, sha256)
    for album_name in albums:
        try:
            escaped_album = album_name.replace('"', '\\"')
            if album_name not in album_cache:
                script = f'''
                tell application "Photos"
                    try
                        set theAlbum to first album whose name is "{escaped_album}"
                    on error
                        set theAlbum to make new album named "{escaped_album}"
                    end try
                    return name of theAlbum
                end tell
                '''
                subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=15,
                )
                album_cache[album_name] = True

            script = f'''
            tell application "Photos"
                set theAlbum to first album whose name is "{escaped_album}"
                set thePhoto to media item id "{uuid}"
                add {{thePhoto}} to theAlbum
            end tell
            '''
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:
            logger.error("Album add failed '%s': %s", album_name, e)


def _import_one(row, conn, album_cache, photos_index: dict[str, str],
                max_retries: int = 2) -> bool:
    path = row["canonical_path"]
    staged_fn = Path(path).name

    # Pre-check: if this filename already exists in Photos.app, skip import
    # and just record the existing UUID
    if staged_fn in photos_index:
        uuid = photos_index[staged_fn]
        _assign_albums_osascript(uuid, row["sha256"], conn, album_cache)
        update_status(conn, row["sha256"], row["source_path"], "imported", photos_uuid=uuid)
        return True

    for attempt in range(max_retries):
        _dismiss_dialog()

        uuid = _osascript_import(path)

        if not uuid:
            time.sleep(1)
            if _dismiss_dialog():
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return False

            # No dialog; check if Photos.app imported it silently (ghost import)
            # Re-query by filename to catch it
            escaped = staged_fn.replace('"', '\\"')
            check_script = f'''
            tell application "Photos"
                set found to search for "{escaped}"
                if (count of found) > 0 then
                    return id of item 1 of found
                else
                    return "NOT_FOUND"
                end if
            end tell
            '''
            try:
                result = subprocess.run(
                    ["osascript", "-e", check_script],
                    capture_output=True, text=True, timeout=15,
                )
                stdout = result.stdout.strip()
                if result.returncode == 0 and stdout and stdout != "NOT_FOUND":
                    uuid = stdout.split("/")[0] if "/" in stdout else stdout
            except Exception:
                pass

        if uuid:
            photos_index[staged_fn] = uuid
            _assign_albums_osascript(uuid, row["sha256"], conn, album_cache)
            update_status(conn, row["sha256"], row["source_path"], "imported", photos_uuid=uuid)
            return True

        if attempt < max_retries - 1:
            time.sleep(2)

    return False


def _restart_photos() -> None:
    console.print("  [yellow]Restarting Photos.app...[/yellow]")
    subprocess.run(["osascript", "-e", 'tell application "Photos" to quit'], capture_output=True, timeout=10)
    time.sleep(5)
    subprocess.run(["open", "-a", "Photos"], capture_output=True)
    time.sleep(10)


def import_photos(
    limit: Optional[int] = None,
    dry_run: bool = False,
    album: Optional[str] = None,
) -> int:
    if not dry_run and not check_photos_running():
        console.print("[red]Photos.app is not running.[/red]")
        console.print("Please open Photos.app before importing.")
        console.print("  [bold]open -a Photos[/bold]")
        raise SystemExit(1)

    conn = get_connection()

    if dry_run:
        files = []
        for status in ("exif_fixed", "staged", "pending"):
            files.extend(get_media_by_status(conn, status, album=album))
        if limit:
            files = files[:limit]
        if not files:
            console.print("[yellow]No files to preview.[/yellow]")
            conn.close()
            return 0
        console.print(f"[bold cyan]DRY RUN: would import {len(files)} files[/bold cyan]")
        for row in files:
            albums = get_album_memberships(conn, row["sha256"])
            album_str = ", ".join(albums) if albums else "(no album)"
            console.print(f"  {row['filename']} -> {album_str}")
        conn.close()
        return len(files)

    files = get_media_by_status(conn, "exif_fixed", limit=limit, album=album)
    if not files:
        console.print("[yellow]No files ready for import. Run stage and fix-exif first.[/yellow]")
        conn.close()
        return 0

    # Build filename index for duplicate prevention
    photos_index = _build_photos_filename_index()

    album_cache: dict = {}
    imported_count = 0
    skipped_count = 0
    failed_count = 0
    consecutive_failures = 0
    total_files = len(files)

    console.print(
        f"Importing [bold]{total_files}[/bold] files one-by-one via osascript "
        f"(timeout: {IMPORT_TIMEOUT}s, 2 retries per file)"
    )

    with Progress(console=console) as progress:
        task = progress.add_task("Importing to Photos.app...", total=total_files)

        for i, row in enumerate(files):
            if not _validate_file(Path(row["canonical_path"])):
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message="File missing or empty")
                failed_count += 1
                progress.advance(task)
                continue

            staged_fn = Path(row["canonical_path"]).name
            already_exists = staged_fn in photos_index

            if _import_one(row, conn, album_cache, photos_index):
                imported_count += 1
                consecutive_failures = 0
                if already_exists:
                    skipped_count += 1
            else:
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message="Rejected by Photos.app")
                failed_count += 1
                consecutive_failures += 1

            progress.advance(task)

            if (i + 1) % COMMIT_INTERVAL == 0:
                conn.commit()

            if (i + 1) % 500 == 0:
                console.print(
                    f"  [dim]Progress: {imported_count} imported"
                    f"{f' ({skipped_count} already existed)' if skipped_count else ''}, "
                    f"{failed_count} failed ({i + 1}/{total_files})[/dim]"
                )

            if consecutive_failures >= 20:
                console.print(f"  [yellow]{consecutive_failures} consecutive failures, restarting Photos.app[/yellow]")
                _restart_photos()
                consecutive_failures = 0

    conn.commit()
    conn.close()

    console.print(f"\n[bold green]Import complete[/bold green]")
    console.print(f"  Imported: {imported_count}")
    if skipped_count:
        console.print(f"  Already existed (deduped): {skipped_count}")
    if failed_count:
        console.print(f"  Failed/rejected: {failed_count}")

    return imported_count
