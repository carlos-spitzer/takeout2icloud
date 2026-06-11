#!/usr/bin/env python3
"""takeout2icloud: Import Google Takeout Photos into macOS Photos.app."""

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from state import (
    get_connection, get_status_counts, get_album_progress,
    is_phase_complete, reset_failed,
)

console = Console()

LOG_DIR = Path.home() / ".takeout2icloud"
LOG_FILE = LOG_DIR / "errors.log"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s\t%(levelname)s\t%(message)s",
        handlers=[
            logging.FileHandler(str(LOG_FILE)),
        ],
    )


@click.group()
def cli():
    """Curate a Google Takeout Photos export and import into macOS Photos.app."""
    _setup_logging()


@cli.command()
@click.argument("takeout_path", type=click.Path(exists=True))
@click.option("--rescan", is_flag=True, help="Wipe DB and scan from scratch.")
def scan(takeout_path: str, rescan: bool):
    """Phase 1: Scan Takeout directory and build the media database."""
    from scanner import scan as do_scan
    do_scan(takeout_path, rescan=rescan)


@cli.command()
def curate():
    """Phase 2: Deduplicate files between album and year bucket folders."""
    from curator import curate as do_curate
    do_curate()


@cli.command()
def stage():
    """Phase 3: Copy pending files to staging directory."""
    from stager import stage as do_stage
    do_stage()


@cli.command("fix-exif")
def fix_exif():
    """Phase 4: Write sidecar metadata into file EXIF tags."""
    from exif_fixer import fix_exif as do_fix
    do_fix()


@cli.command("import")
@click.option("--limit", type=int, default=None, help="Max files to import.")
@click.option("--dry-run", is_flag=True, help="Preview without importing.")
@click.option("--album", type=str, default=None, help="Only import files in this album.")
def import_cmd(limit, dry_run, album):
    """Phase 5: Import EXIF-fixed files into Photos.app."""
    from importer import import_photos
    import_photos(limit=limit, dry_run=dry_run, album=album)


@cli.command()
@click.argument("takeout_path", type=click.Path(exists=True))
@click.option("--limit", type=int, default=None, help="Max files to import.")
@click.option("--dry-run", is_flag=True, help="Preview import without touching Photos.app.")
@click.option("--rescan", is_flag=True, help="Wipe DB and scan from scratch.")
def run(takeout_path: str, limit, dry_run, rescan):
    """Run all phases in sequence: scan, curate, stage, fix-exif, import.

    Fully resumable: re-running picks up where it left off.
    Each phase only processes files that haven't reached that phase yet.
    """
    conn = get_connection()
    counts = get_status_counts(conn)

    # Phase 1: Scan (always runs incrementally, skips known files)
    if rescan or not counts:
        console.print("[bold]Phase 1: Scanning...[/bold]")
        conn.close()
        from scanner import scan as do_scan
        do_scan(takeout_path, rescan=rescan)
    else:
        total = sum(counts.values())
        console.print(f"[dim]Phase 1: Scan, {total} files in DB. Running incremental scan for new files...[/dim]")
        conn.close()
        from scanner import scan as do_scan
        do_scan(takeout_path, rescan=False)

    # Phase 2: Curate (idempotent, only touches pending files)
    conn = get_connection()
    pending_count = get_status_counts(conn).get("pending", 0)
    conn.close()
    if pending_count > 0:
        console.print(f"\n[bold]Phase 2: Curating ({pending_count} pending files)...[/bold]")
        from curator import curate as do_curate
        do_curate()
    else:
        console.print("[dim]Phase 2: Curate skipped (no pending files to curate)[/dim]")

    # Phase 3: Stage (only stages pending files)
    conn = get_connection()
    pending_count = get_status_counts(conn).get("pending", 0)
    conn.close()
    if pending_count > 0:
        console.print(f"\n[bold]Phase 3: Staging ({pending_count} files)...[/bold]")
        from stager import stage as do_stage
        do_stage()
    else:
        console.print("[dim]Phase 3: Stage skipped (no pending files)[/dim]")

    # Phase 4: Fix EXIF (only fixes staged files)
    conn = get_connection()
    staged_count = get_status_counts(conn).get("staged", 0)
    conn.close()
    if staged_count > 0:
        console.print(f"\n[bold]Phase 4: Fixing EXIF ({staged_count} files)...[/bold]")
        from exif_fixer import fix_exif as do_fix
        do_fix()
    else:
        console.print("[dim]Phase 4: Fix EXIF skipped (no staged files)[/dim]")

    # Phase 5: Import (only imports exif_fixed files)
    conn = get_connection()
    ready_count = get_status_counts(conn).get("exif_fixed", 0)
    conn.close()
    if ready_count > 0:
        console.print(f"\n[bold]Phase 5: Importing ({ready_count} files)...[/bold]")
        from importer import import_photos
        import_photos(limit=limit, dry_run=dry_run)
    else:
        console.print("[dim]Phase 5: Import skipped (no exif_fixed files)[/dim]")

    console.print("\n[bold green]All phases complete.[/bold green]")
    _print_status()


@cli.command()
@click.option("--phase", type=click.Choice(["stage", "fix-exif", "import"]),
              default=None, help="Only retry failures from this phase.")
def retry(phase):
    """Reset failed files so they can be reprocessed.

    Without --phase, resets all failed files back to 'pending'.
    With --phase, resets only failures from that specific phase.
    """
    conn = get_connection()
    counts = get_status_counts(conn)
    failed = counts.get("failed", 0)

    if failed == 0:
        console.print("[green]No failed files to retry.[/green]")
        conn.close()
        return

    count = reset_failed(conn, phase)
    conn.close()

    if count > 0:
        scope = f"from {phase}" if phase else "across all phases"
        console.print(f"[bold green]Reset {count} failed files {scope} for retry.[/bold green]")
        console.print("Run the appropriate command (or 'run') to reprocess them.")
    else:
        console.print("[yellow]No matching failed files found for that phase.[/yellow]")


@cli.command("repair-uuids")
@click.option("--dry-run", is_flag=True, help="Show what would be repaired without changing the DB.")
def repair_uuids_cmd(dry_run):
    """Repair invalid photo UUIDs in the state DB.

    Some photos were imported with incorrect UUIDs due to filename collisions
    during batch matching. This command cross-references Photos.app to find
    the correct UUIDs and updates the DB.
    """
    from album_sync import repair_uuids
    repair_uuids(dry_run=dry_run)


@cli.command("sync-albums")
@click.option("--dry-run", is_flag=True, help="Audit only, don't make changes.")
@click.option("--album", type=str, default=None, help="Filter albums by name (substring match).")
@click.option("--skip-removals", is_flag=True, help="Only add missing photos, don't remove extras.")
def sync_albums_cmd(dry_run, album, skip_removals):
    """Sync iCloud album memberships to match Google Takeout.

    Compares expected assignments (from the state DB) against actual albums
    in Photos.app, then adds missing photos and removes extras.
    """
    from album_sync import sync_albums
    sync_albums(album_filter=album, dry_run=dry_run, skip_removals=skip_removals)


@cli.command()
def status():
    """Show current import progress."""
    _print_status()


def _print_status() -> None:
    conn = get_connection()
    counts = get_status_counts(conn)

    if not counts:
        console.print("[yellow]No data yet. Run 'takeout2icloud scan' first.[/yellow]")
        conn.close()
        return

    table = Table(title="Import Status")
    table.add_column("Status", style="cyan")
    table.add_column("Count", style="white", justify="right")

    status_order = ["pending", "staged", "exif_fixed", "imported", "duplicate", "failed"]
    for s in status_order:
        if s in counts:
            style = {
                "imported": "green",
                "failed": "red",
                "duplicate": "dim",
            }.get(s, "white")
            table.add_row(s, f"[{style}]{counts[s]}[/{style}]")

    total = sum(counts.values())
    table.add_row("TOTAL", str(total), style="bold")
    console.print(table)

    albums = get_album_progress(conn)
    if albums:
        album_table = Table(title="Album Progress")
        album_table.add_column("Album", style="cyan")
        album_table.add_column("Imported", justify="right")
        album_table.add_column("Total", justify="right")
        album_table.add_column("Progress", justify="right")

        for a in albums:
            pct = (a["imported"] / a["total"] * 100) if a["total"] > 0 else 0
            album_table.add_row(
                a["album_name"],
                str(a["imported"]),
                str(a["total"]),
                f"{pct:.0f}%",
            )
        console.print(album_table)

    remaining = counts.get("pending", 0) + counts.get("staged", 0) + counts.get("exif_fixed", 0)
    failed = counts.get("failed", 0)
    if remaining > 0:
        console.print(f"\n[bold]Remaining files to process: {remaining}[/bold]")
    if failed > 0:
        console.print(f"[red]Failed files: {failed}[/red] (use 'takeout2icloud retry' to reset them)")

    conn.close()


def main():
    cli()


if __name__ == "__main__":
    main()
