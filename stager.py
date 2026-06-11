import logging
import shutil
from pathlib import Path

from rich.console import Console
from rich.progress import Progress

from state import get_connection, get_media_by_status, update_status, update_canonical_path

logger = logging.getLogger(__name__)
console = Console()

STAGING_DIR = Path.home() / ".takeout2icloud" / "staging"


def stage() -> int:
    conn = get_connection()
    pending = get_media_by_status(conn, "pending")

    if not pending:
        console.print("[yellow]No pending files to stage.[/yellow]")
        conn.close()
        return 0

    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    staged_count = 0
    used_names: dict[str, int] = {}

    # Pre-populate used_names with files already in staging dir
    for existing in STAGING_DIR.iterdir():
        if existing.is_file():
            used_names[existing.name] = 0

    with Progress(console=console) as progress:
        task = progress.add_task("Staging files...", total=len(pending))

        for row in pending:
            src = Path(row["source_path"])
            if not src.exists():
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message=f"Source file not found: {src}")
                progress.advance(task)
                continue

            album = row.get("album_name") or "unalbumized"
            safe_album = album.replace("/", "_").replace(" ", "_")
            dest_name = f"{safe_album}__{src.name}"
            if dest_name in used_names:
                used_names[dest_name] += 1
                stem = src.stem
                ext = src.suffix
                dest_name = f"{safe_album}__{stem}_{used_names[dest_name]}{ext}"
            else:
                used_names[dest_name] = 0

            dest = STAGING_DIR / dest_name

            try:
                shutil.copy2(str(src), str(dest))
                update_canonical_path(conn, row["sha256"], row["source_path"], str(dest))
                update_status(conn, row["sha256"], row["source_path"], "staged")
                staged_count += 1
            except OSError as e:
                update_status(conn, row["sha256"], row["source_path"],
                              "failed", error_message=str(e))
                logger.error("Failed to stage %s: %s", src, e)

            progress.advance(task)

            if staged_count % 200 == 0:
                conn.commit()

    conn.commit()
    conn.close()

    console.print(f"[bold green]Staged {staged_count} files[/bold green] to {STAGING_DIR}")
    return staged_count
