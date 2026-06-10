"""Phase 5: Import EXIF-fixed files into macOS Photos.app.

Uses photoscript (osxphotos) to drive Photos.app via Apple Events.
A separate bash process (dismiss_dialogs.sh) auto-dismisses error dialogs
to avoid the Apple Events deadlock that occurs when a background thread
tries to interact with Photos.app while an import call is in progress.

When Photos.app shows an error dialog mid-batch, import_photos() returns
an empty list. Rather than marking those files as failed, this module
leaves them as exif_fixed so a subsequent run can retry them. Only files
that are explicitly unmatched in a partially-successful batch are marked
as genuinely rejected.
"""

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress

from state import get_connection, get_media_by_status, get_album_memberships, update_status

logger = logging.getLogger(__name__)
console = Console()

BATCH_SIZE = 10

# Path to the standalone dismiss script (runs as separate OS process)
_DISMISS_SCRIPT = Path(__file__).parent / "dismiss_dialogs.sh"
_DISMISS_LOG = Path.home() / ".takeout2icloud" / "dismiss.log"


def check_photoscript() -> None:
    """Verify that photoscript (from osxphotos) is installed."""
    try:
        import photoscript  # noqa: F401
    except ImportError:
        console.print("[red]photoscript not found.[/red]")
        console.print("Install it with: [bold]pip install osxphotos[/bold]")
        raise SystemExit(1)


def check_photos_running() -> bool:
    """Return True if Photos.app is currently running."""
    result = subprocess.run(["pgrep", "-x", "Photos"], capture_output=True)
    return result.returncode == 0


def _validate_file(filepath: Path) -> bool:
    """Return True if the file exists and is non-empty."""
    return filepath.exists() and filepath.stat().st_size > 0


class DialogDismisser:
    """Launches dismiss_dialogs.sh as a separate OS process.

    This avoids the Apple Events deadlock that occurs when a background
    THREAD tries to send osascript commands while the main thread's
    import_photos() call is waiting for Photos.app to respond.

    A separate PROCESS has its own Apple Events connection, so it can
    interact with System Events independently.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None

    def start(self):
        _DISMISS_LOG.parent.mkdir(parents=True, exist_ok=True)
        _DISMISS_LOG.write_text("")

        self._proc = subprocess.Popen(
            [str(_DISMISS_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        logger.info("Dialog dismisser started (PID %d)", self._proc.pid)

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                self._proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            logger.info("Dialog dismisser stopped")

    @property
    def total_dismissed(self) -> int:
        """Count dismissed dialogs by reading the log file."""
        try:
            if _DISMISS_LOG.exists():
                return sum(1 for line in _DISMISS_LOG.read_text().splitlines() if line.strip())
        except Exception:
            pass
        return 0


def _assign_albums(photo, sha256: str, conn, album_cache: dict, photos_lib) -> None:
    """Add a photo to all albums it belongs to, creating albums as needed."""
    albums = get_album_memberships(conn, sha256)
    for album_name in albums:
        try:
            if album_name not in album_cache:
                existing = photos_lib.album(album_name)
                album_cache[album_name] = existing if existing else photos_lib.create_album(album_name)
            album_cache[album_name].add([photo])
        except Exception as e:
            logger.error("Album add failed '%s': %s", album_name, e)


def _match_imported_photos(imported, batch_rows, rows_by_basename, conn, album_cache, photos_lib):
    """Match returned Photo objects to DB rows by filename. Returns set of matched source_paths."""
    matched_sources: set[str] = set()

    for photo in imported:
        try:
            photo_fn = photo.filename
        except Exception:
            continue

        matching_rows = rows_by_basename.get(photo_fn, [])
        if not matching_rows:
            # Fallback: match by stem (Photos.app may change extension)
            photo_stem = Path(photo_fn).stem
            for basename, rows_list in rows_by_basename.items():
                if Path(basename).stem == photo_stem:
                    matching_rows = rows_list
                    break

        for row in matching_rows:
            if row["source_path"] in matched_sources:
                continue
            matched_sources.add(row["source_path"])
            _assign_albums(photo, row["sha256"], conn, album_cache, photos_lib)
            update_status(conn, row["sha256"], row["source_path"], "imported", photos_uuid=photo.uuid)
            break

    return matched_sources


def import_photos(
    limit: Optional[int] = None,
    dry_run: bool = False,
    album: Optional[str] = None,
) -> int:
    """Import EXIF-fixed files into Photos.app.

    Returns the number of files successfully imported.
    """
    check_photoscript()

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

    import photoscript

    photos_lib = photoscript.PhotosLibrary()

    # Verify Apple Events authorization
    try:
        photos_lib.albums()
    except Exception as e:
        err = str(e)
        if "-1743" in err or "not authorized" in err.lower():
            console.print("[red]Apple Events authorization required.[/red]")
            console.print(
                "macOS is blocking this process from controlling Photos.app.\n"
                "Grant access in System Settings > Privacy > Automation."
            )
            raise SystemExit(1)
        raise

    # Start background dialog dismisser
    dismisser = DialogDismisser()
    dismisser.start()
    console.print("[dim]Background dialog dismisser started[/dim]")

    album_cache: dict = {}
    imported_count = 0
    failed_count = 0
    deferred_count = 0
    total_files = len(files)
    total_batches = (total_files + BATCH_SIZE - 1) // BATCH_SIZE
    consecutive_empty = 0

    console.print(
        f"Importing [bold]{total_files}[/bold] files in "
        f"[bold]{total_batches}[/bold] batches of {BATCH_SIZE}"
    )

    with Progress(console=console) as progress:
        task = progress.add_task("Importing to Photos.app...", total=total_files)

        for batch_start in range(0, total_files, BATCH_SIZE):
            batch = files[batch_start:batch_start + BATCH_SIZE]

            # Validate paths
            batch_rows: list[dict] = []
            for row in batch:
                if _validate_file(Path(row["canonical_path"])):
                    batch_rows.append(row)
                else:
                    update_status(conn, row["sha256"], row["source_path"],
                                  "failed", error_message="File missing or empty")
                    failed_count += 1
                    progress.advance(task)

            if not batch_rows:
                continue

            batch_paths = [r["canonical_path"] for r in batch_rows]
            rows_by_basename: dict[str, list[dict]] = {}
            for row in batch_rows:
                basename = Path(row["canonical_path"]).name
                rows_by_basename.setdefault(basename, []).append(row)

            try:
                imported = photos_lib.import_photos(batch_paths, skip_duplicate_check=True)
                time.sleep(0.5)

                matched_sources = _match_imported_photos(
                    imported, batch_rows, rows_by_basename, conn, album_cache, photos_lib,
                )
                imported_count += len(matched_sources)

                # Handle unmatched files
                unmatched = [r for r in batch_rows if r["source_path"] not in matched_sources]
                if unmatched and len(imported) == 0:
                    # Entire batch returned empty: dialog likely blocked the result.
                    # Leave as exif_fixed for retry.
                    consecutive_empty += 1
                    deferred_count += len(unmatched)
                    if consecutive_empty >= 5:
                        console.print("[yellow]Multiple empty batches, waiting for Photos.app...[/yellow]")
                        time.sleep(10)
                        consecutive_empty = 0
                else:
                    consecutive_empty = 0
                    for row in unmatched:
                        update_status(conn, row["sha256"], row["source_path"],
                                      "failed", error_message="Rejected by Photos.app (confirmed)")
                        failed_count += 1

                progress.advance(task, len(batch_rows))

            except Exception as e:
                logger.error("Batch import error: %s", e)
                # Leave as exif_fixed for retry
                deferred_count += len(batch_rows)
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    time.sleep(15)
                    consecutive_empty = 0
                progress.advance(task, len(batch_rows))

            conn.commit()

            batch_idx = batch_start // BATCH_SIZE
            if (batch_idx + 1) % 100 == 0:
                console.print(
                    f"  [dim]Progress: {imported_count} imported, "
                    f"{failed_count} failed, {deferred_count} deferred, "
                    f"{total_files - imported_count - failed_count - deferred_count} remaining "
                    f"(dialogs dismissed: {dismisser.total_dismissed})[/dim]"
                )

    dismisser.stop()
    conn.close()

    console.print(f"\n[bold green]Import complete[/bold green]")
    console.print(f"  Imported: {imported_count}")
    if deferred_count:
        console.print(f"  Deferred (retry needed): {deferred_count}")
    if failed_count:
        console.print(f"  Failed/rejected: {failed_count}")
    console.print(f"  Dialogs auto-dismissed: {dismisser.total_dismissed}")

    if deferred_count:
        console.print("\n[yellow]Some files were deferred due to Photos.app dialogs.[/yellow]")
        console.print("Run [bold]takeout2icloud import[/bold] again to retry them.")

    return imported_count
