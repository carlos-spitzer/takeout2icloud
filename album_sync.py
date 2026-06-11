"""Sync album memberships between the Google Takeout DB and macOS Photos.app.

Compares expected album assignments (from the SQLite state DB) against actual
assignments in Photos.app, then adds missing photos and removes extras so that
iCloud albums match the Google Photos source of truth.

Includes a UUID repair step: some photos were imported with incorrect UUIDs
stored in the DB. The repair step builds a filename->UUID map from Photos.app
and fixes mismatches.
"""

import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from state import get_connection

console = Console()

_NAME_OVERRIDES: dict[str, str] = {
    "Granja _El Enebral_": "Granja  El Enebral",
    "Granja _Giraluna_": "Granja  Giraluna",
}


def _photos_name(db_name: str) -> str:
    return _NAME_OVERRIDES.get(db_name, db_name)


def _osascript(script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _bulk_get_property(prop: str) -> list[str]:
    """Get a property of every media item from Photos.app in one call."""
    script = f'''
    tell application "Photos"
        set vals to {prop} of every media item
        set output to ""
        repeat with v in vals
            set output to output & v & linefeed
        end repeat
        return output
    end tell
    '''
    result = _osascript(script, timeout=300)
    return [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]


def _build_photos_index() -> tuple[dict[str, list[str]], set[str]]:
    """Build filename->UUIDs map and full UUID set from Photos.app.

    Returns (filename_to_uuids, all_uuids). Filenames are stored in their
    original case; use _case_insensitive_lookup for matching.
    """
    console.print("[dim]Loading Photos.app library index...[/dim]")
    ids = _bulk_get_property("id")
    filenames = _bulk_get_property("filename")

    if len(ids) != len(filenames):
        console.print(f"[red]Mismatch: {len(ids)} IDs vs {len(filenames)} filenames[/red]")
        return {}, set()

    fn_to_uuids: dict[str, list[str]] = {}
    all_uuids: set[str] = set()
    for raw_id, fn in zip(ids, filenames):
        base = raw_id.split("/")[0] if "/" in raw_id else raw_id
        all_uuids.add(base)
        fn_to_uuids.setdefault(fn, []).append(base)

    console.print(f"[dim]Indexed {len(all_uuids)} media items, {len(fn_to_uuids)} unique filenames[/dim]")
    return fn_to_uuids, all_uuids


def _case_insensitive_lookup(fn_to_uuids: dict[str, list[str]], filename: str) -> list[str]:
    """Look up UUIDs for a filename, trying exact match then case-insensitive."""
    if filename in fn_to_uuids:
        return fn_to_uuids[filename]
    lower = filename.lower()
    for k, v in fn_to_uuids.items():
        if k.lower() == lower:
            return v
    return []


def repair_uuids(dry_run: bool = False) -> int:
    """Find and fix invalid UUIDs in the state DB.

    Compares DB photos_uuid values against Photos.app, then uses filename
    matching to find the correct UUID for mismatches.
    Returns the number of repaired UUIDs.
    """
    fn_to_uuids, all_uuids = _build_photos_index()
    if not all_uuids:
        console.print("[red]Could not load Photos.app index[/red]")
        return 0

    conn = get_connection()
    rows = conn.execute("""
        SELECT sha256, source_path, canonical_path, filename, photos_uuid
        FROM media
        WHERE status = 'imported' AND photos_uuid IS NOT NULL
    """).fetchall()

    # Build set of valid UUIDs already correctly assigned in the DB
    valid_assigned = set()
    for r in rows:
        if r["photos_uuid"] in all_uuids:
            valid_assigned.add(r["photos_uuid"])

    invalid = [dict(r) for r in rows if r["photos_uuid"] not in all_uuids]

    console.print(f"Found [bold]{len(invalid)}[/bold] photos with invalid UUIDs out of {len(rows)}")

    if not invalid:
        console.print("[green]All UUIDs are valid.[/green]")
        conn.close()
        return 0

    repaired = 0
    not_in_photos = 0
    ambiguous = 0

    for item in invalid:
        staging_fn = Path(item["canonical_path"]).name
        candidates = _case_insensitive_lookup(fn_to_uuids, staging_fn)

        if not candidates:
            # Try original filename too
            candidates = _case_insensitive_lookup(fn_to_uuids, item["filename"])

        if not candidates:
            not_in_photos += 1
            continue

        if len(candidates) == 1:
            new_uuid = candidates[0]
            if not dry_run:
                conn.execute(
                    "UPDATE media SET photos_uuid = ? WHERE sha256 = ? AND source_path = ?",
                    (new_uuid, item["sha256"], item["source_path"]),
                )
            repaired += 1
            valid_assigned.add(new_uuid)
            continue

        # Multiple candidates; pick one not already claimed
        unclaimed = [c for c in candidates if c not in valid_assigned]
        if len(unclaimed) == 1:
            new_uuid = unclaimed[0]
            if not dry_run:
                conn.execute(
                    "UPDATE media SET photos_uuid = ? WHERE sha256 = ? AND source_path = ?",
                    (new_uuid, item["sha256"], item["source_path"]),
                )
            repaired += 1
            valid_assigned.add(new_uuid)
        else:
            ambiguous += 1

    if not dry_run:
        conn.commit()

    conn.close()

    console.print(
        f"  Repaired: [green]{repaired}[/green], "
        f"not in Photos.app: [yellow]{not_in_photos}[/yellow], "
        f"ambiguous: [yellow]{ambiguous}[/yellow]"
    )
    if dry_run:
        console.print("[bold cyan]DRY RUN, no DB changes made.[/bold cyan]")
    return repaired


def _get_album_uuids(album_name: str) -> Optional[set[str]]:
    """Query Photos.app for all media item UUIDs in an album."""
    escaped = album_name.replace('"', '\\"')
    script = f'''
    tell application "Photos"
        try
            set theAlbum to first album whose name is "{escaped}"
            set uuidList to id of every media item of theAlbum
            set output to ""
            repeat with u in uuidList
                set output to output & u & linefeed
            end repeat
            return output
        on error
            return "ALBUM_NOT_FOUND"
        end try
    end tell
    '''
    result = _osascript(script)
    raw = result.stdout.strip()
    if result.returncode != 0 or raw == "ALBUM_NOT_FOUND":
        return None
    if not raw:
        return set()
    uuids = set()
    for u in raw.split("\n"):
        u = u.strip()
        if not u:
            continue
        base = u.split("/")[0] if "/" in u else u
        uuids.add(base)
    return uuids


def _get_expected_uuids(conn, db_name: str) -> set[str]:
    """Get expected UUIDs from the state DB, trying both NFD and NFC normalization."""
    for norm_form in ("NFD", "NFC"):
        normalized = unicodedata.normalize(norm_form, db_name)
        rows = conn.execute("""
            SELECT DISTINCT m.photos_uuid
            FROM album_memberships am
            JOIN media m ON am.sha256 = m.sha256
            WHERE am.album_name = ?
              AND m.status = 'imported'
              AND m.photos_uuid IS NOT NULL
        """, (normalized,)).fetchall()
        if rows:
            return set(
                r["photos_uuid"] for r in rows
                if not r["photos_uuid"].endswith("/L0/001")
            )
    return set()


def _add_photo_to_album(album_name: str, uuid: str) -> bool:
    escaped = album_name.replace('"', '\\"')
    script = f'''
    tell application "Photos"
        set theAlbum to first album whose name is "{escaped}"
        set thePhoto to media item id "{uuid}"
        add {{thePhoto}} to theAlbum
        return "ok"
    end tell
    '''
    result = _osascript(script, timeout=30)
    return result.returncode == 0 and "ok" in result.stdout


def _remove_photo_from_album(album_name: str, uuid: str) -> bool:
    escaped = album_name.replace('"', '\\"')
    script = f'''
    tell application "Photos"
        set theAlbum to first album whose name is "{escaped}"
        set thePhoto to media item id "{uuid}"
        remove {{thePhoto}} from theAlbum
        return "ok"
    end tell
    '''
    result = _osascript(script, timeout=30)
    return result.returncode == 0


def _get_all_album_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT album_name FROM album_memberships ORDER BY album_name"
    ).fetchall()
    return [r["album_name"] for r in rows]


def audit_albums(
    album_filter: Optional[str] = None,
    valid_uuids: Optional[set[str]] = None,
) -> list[dict]:
    """Compare expected vs actual for all albums. Returns list of diffs.

    If valid_uuids is provided, "missing" counts exclude UUIDs that don't
    exist in Photos.app (those need re-import, not album sync).
    """
    conn = get_connection()
    albums = _get_all_album_names(conn)
    results = []

    for db_name in albums:
        if album_filter:
            nfd_filter = unicodedata.normalize("NFD", album_filter).lower()
            nfd_name = unicodedata.normalize("NFD", db_name).lower()
            if nfd_filter not in nfd_name:
                continue

        photos_name = _photos_name(db_name)
        expected = _get_expected_uuids(conn, db_name)
        if not expected:
            results.append({
                "db_name": db_name, "photos_name": photos_name,
                "expected": 0, "actual": 0, "missing": 0, "extra": 0,
                "phantom": 0, "status": "no_data",
            })
            continue

        actual = _get_album_uuids(photos_name)
        if actual is None:
            phantom = len(expected - valid_uuids) if valid_uuids else 0
            results.append({
                "db_name": db_name, "photos_name": photos_name,
                "expected": len(expected), "actual": 0,
                "missing": len(expected) - phantom, "extra": 0,
                "phantom": phantom, "status": "not_found",
            })
            continue

        raw_missing = expected - actual
        extra = actual - expected

        # Split missing into actionable (UUID exists) vs phantom (UUID invalid)
        if valid_uuids:
            actionable_missing = raw_missing & valid_uuids
            phantom = raw_missing - valid_uuids
        else:
            actionable_missing = raw_missing
            phantom = set()

        status = "ok" if not actionable_missing and not extra else "drift"

        results.append({
            "db_name": db_name, "photos_name": photos_name,
            "expected": len(expected), "actual": len(actual),
            "missing": len(actionable_missing), "extra": len(extra),
            "phantom": len(phantom),
            "missing_uuids": actionable_missing, "extra_uuids": extra,
            "status": status,
        })

    conn.close()
    return results


def sync_albums(
    album_filter: Optional[str] = None,
    dry_run: bool = False,
    skip_removals: bool = False,
) -> None:
    """Sync all album memberships: add missing, remove extras."""

    # Step 1: Repair invalid UUIDs
    console.print("[bold]Step 1: Repairing invalid UUIDs...[/bold]\n")
    repair_uuids(dry_run=dry_run)

    # Build the valid UUID set for filtering phantom entries in the audit
    console.print("\n[dim]Building validation index...[/dim]")
    _, valid_uuids = _build_photos_index()

    # Step 2: Audit
    console.print("\n[bold]Step 2: Auditing albums...[/bold]")
    results = audit_albums(album_filter, valid_uuids=valid_uuids)

    albums_ok = sum(1 for r in results if r["status"] == "ok")
    albums_drift = [r for r in results if r["status"] == "drift"]
    albums_not_found = [r for r in results if r["status"] == "not_found"]
    albums_nodata = [r for r in results if r["status"] == "no_data"]

    total_to_add = sum(r["missing"] for r in albums_drift)
    total_to_remove = sum(r["extra"] for r in albums_drift)
    total_phantom = sum(r["phantom"] for r in results)

    table = Table(title="Album Audit (showing drift only)")
    table.add_column("Album", style="cyan")
    table.add_column("Expected", justify="right")
    table.add_column("In iCloud", justify="right")
    table.add_column("To Add", justify="right", style="red")
    table.add_column("To Remove", justify="right", style="yellow")
    table.add_column("Phantom", justify="right", style="dim")

    for r in results:
        if r["status"] == "ok" or r["status"] == "no_data":
            continue
        table.add_row(
            r["db_name"], str(r["expected"]), str(r["actual"]),
            str(r["missing"]) if r["missing"] else "-",
            str(r["extra"]) if r["extra"] else "-",
            str(r["phantom"]) if r["phantom"] else "-",
        )

    console.print(table)
    console.print(
        f"\n[bold]{albums_ok}[/bold] OK, "
        f"[bold red]{len(albums_drift)}[/bold red] drift, "
        f"[bold yellow]{len(albums_not_found)}[/bold yellow] not found"
    )
    console.print(
        f"Actionable: [red]+{total_to_add} to add[/red], "
        f"[yellow]-{total_to_remove} to remove[/yellow]"
    )
    if total_phantom:
        console.print(f"[dim]{total_phantom} phantom entries (invalid UUIDs, need re-import)[/dim]")

    if not albums_drift:
        console.print("\n[green]All albums are in sync.[/green]")
        return

    if dry_run:
        console.print("\n[bold cyan]DRY RUN, no changes made.[/bold cyan]")
        return

    # Step 3: Apply changes
    console.print("\n[bold]Step 3: Applying changes...[/bold]")
    total_added = 0
    total_removed = 0
    total_add_failed = 0
    total_remove_failed = 0

    for i, r in enumerate(albums_drift, 1):
        photos_name = r["photos_name"]
        missing = r.get("missing_uuids", set())
        extra = r.get("extra_uuids", set())
        console.print(f"\n[dim]({i}/{len(albums_drift)})[/dim] [cyan]{photos_name}[/cyan]")

        if missing:
            console.print(f"  Adding {len(missing)} photos...")
            added = 0
            failed = 0
            for uuid in missing:
                if _add_photo_to_album(photos_name, uuid):
                    added += 1
                else:
                    failed += 1
                if (added + failed) % 50 == 0:
                    console.print(f"    ... {added}/{len(missing)} added, {failed} failed", style="dim")
                    time.sleep(0.3)
            total_added += added
            total_add_failed += failed
            console.print(f"  [green]+{added}[/green]" + (f" [red]({failed} failed)[/red]" if failed else ""))

        if extra and not skip_removals:
            console.print(f"  Removing {len(extra)} photos...")
            removed = 0
            failed = 0
            for uuid in extra:
                if _remove_photo_from_album(photos_name, uuid):
                    removed += 1
                else:
                    failed += 1
                if (removed + failed) % 50 == 0:
                    console.print(f"    ... {removed}/{len(extra)} removed, {failed} failed", style="dim")
                    time.sleep(0.3)
            total_removed += removed
            total_remove_failed += failed
            console.print(f"  [yellow]-{removed}[/yellow]" + (f" [red]({failed} failed)[/red]" if failed else ""))

    console.print(f"\n[bold green]Sync complete[/bold green]")
    console.print(f"  Added: {total_added}" + (f" ({total_add_failed} failed)" if total_add_failed else ""))
    if not skip_removals:
        console.print(f"  Removed: {total_removed}" + (f" ({total_remove_failed} failed)" if total_remove_failed else ""))
    if total_add_failed or total_remove_failed:
        console.print("\n[yellow]Run sync-albums again to retry failed operations.[/yellow]")
