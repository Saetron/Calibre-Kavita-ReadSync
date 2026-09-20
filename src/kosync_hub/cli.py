"""Command-line interface for kosync-hub."""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

import click
import uvicorn
from rich.console import Console
from rich.table import Table

from .config import AppConfig, load_config
from .db import InternalDatabase
from .clients.calibre_db import CalibreDbClient
from .clients.kavita import KavitaClient
from .server import create_app
from .synchronizer import Synchronizer

console = Console()


def get_config(ctx: click.Context, config_path: Optional[str] = None) -> AppConfig:
    """Resolves configuration from command option or context."""
    if config_path:
        return load_config(config_path)
    if ctx.obj and "config" in ctx.obj and ctx.obj["config"]:
        return ctx.obj["config"]
    return load_config(None)


def init_components(config: AppConfig):
    """Initializes database, clients, and synchronizer from configuration."""
    db_file = Path(config.data_dir) / "kosync_hub.sqlite3"
    db = InternalDatabase(str(db_file))

    kavita = None
    if config.kavita.enabled:
        kavita = KavitaClient(
            base_url=config.kavita.base_url,
            api_key=config.kavita.api_key,
            timeout=config.kavita.timeout,
            db=db,
        )

    calibre = None
    if config.calibre.enabled:
        calibre = CalibreDbClient(
            library_path=config.calibre.library_path,
            read_pct_column=config.calibre.read_pct_column,
            read_status_column=config.calibre.read_status_column,
            last_read_column=config.calibre.last_read_column,
            progress_column=config.calibre.progress_column,
            auto_create_columns=config.calibre.auto_create_columns,
            mark_read_threshold=config.calibre.mark_read_threshold,
            internal_db=db,
        )

    sync = Synchronizer(
        db=db,
        kavita=kavita,
        calibre=calibre,
        conflict_strategy=config.sync.conflict_resolution,
        interval_seconds=config.sync.interval_seconds,
    )

    return db, kavita, calibre, sync


@click.group()
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose debug logging.")
@click.pass_context
def cli(ctx, config_path: Optional[str], verbose: bool = False):
    """KOReader Multi-Sync Hub: Sync reading progress between Kavita and Calibre."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if verbose:
        logging.getLogger("kosync_hub").setLevel(logging.DEBUG)
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command("serve")
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.pass_context
def serve(ctx, config_path: Optional[str] = None):
    """Starts the KOReader sync server and background synchronizer."""
    config: AppConfig = get_config(ctx, config_path)
    db, kavita, calibre, sync = init_components(config)

    console.print(f"[bold cyan]Starting KOReader Multi-Sync Hub on {config.server.host}:{config.server.port}[/bold cyan]")
    app = create_app(config=config, db=db, synchronizer=sync)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


@cli.command("run")
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.pass_context
def run(ctx, config_path: Optional[str] = None):
    """Runs the bidirectional sync daemon in the foreground."""
    serve.callback(config_path=config_path)


@cli.command("sync-now")
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.pass_context
def sync_now(ctx, config_path: Optional[str] = None):
    """Performs an immediate one-shot synchronization pass between Kavita and Calibre."""
    config: AppConfig = get_config(ctx, config_path)
    _, _, _, sync = init_components(config)

    async def _run():
        console.print("[bold yellow]Running synchronization pass...[/bold yellow]")
        result = await sync.sync_all()
        console.print(
            f"[bold green]Sync pass completed![/bold green] "
            f"Updated Kavita: {result['updated_kavita']}, Updated Calibre: {result['updated_calibre']}."
        )

    asyncio.run(_run())


@cli.command("test-connections")
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.pass_context
def test_connections(ctx, config_path: Optional[str] = None):
    """Verifies credentials, connectivity, and book discovery for Kavita and Calibre."""
    config: AppConfig = get_config(ctx, config_path)
    _, kavita, calibre, _ = init_components(config)

    async def _test():
        console.print("\n[bold]Testing Service Connections:[/bold]")
        if kavita:
            with console.status("Checking Kavita API & Recent Reads..."):
                ok = await kavita.test_connection()
                reads = await kavita.get_on_deck_reads() if ok else []
            if ok:
                console.print(f"[green]✔ Kavita:[/green] Connected to {kavita.koreader_url}")
                jwt_status = "active (Bearer JWT)" if kavita.jwt_token else "fallback (x-api-key)"
                console.print(f"  [dim]Authentication mode: {jwt_status}[/dim]")
                console.print(f"  [cyan]Recent Reads Discovered:[/cyan] {len(reads)} book(s) with Calibre IDs")
                for r in reads:
                    console.print(
                        f"    • [bold]{r.series_name}[/bold] -> Calibre ID: [blue]#{r.calibre_id}[/blue] "
                        f"({round(r.percentage * 100, 1)}% read)"
                    )
            else:
                console.print(f"[red]✖ Kavita:[/red] Failed to connect/authenticate at {kavita.koreader_url}")
        else:
            console.print("[yellow]– Kavita:[/yellow] Disabled in config")

        if calibre:
            with console.status("Checking Calibre Database..."):
                ok = await calibre.test_connection()
            if ok:
                console.print(f"[green]✔ Calibre DB:[/green] Successfully connected to {calibre.db_path}")
                recent_calibre = calibre.get_recently_read_books(limit=5)
                console.print(f"  [cyan]Calibre Books with Progress:[/cyan] {len(recent_calibre)} recent book(s)")
                for b in recent_calibre:
                    console.print(
                        f"    • [bold]{b.title}[/bold] -> ID: [blue]#{b.book_id}[/blue] "
                        f"({round(b.percentage * 100, 1)}% read)"
                    )
            else:
                console.print(f"[red]✖ Calibre DB:[/red] Failed to open database at {calibre.db_path}")
        else:
            console.print("[yellow]– Calibre:[/yellow] Disabled in config")

    asyncio.run(_test())


@cli.command("scan-calibre")
@click.option("--limit", "-l", default=None, type=int, help="Limit number of books to scan.")
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.pass_context
def scan_calibre(ctx, limit: Optional[int], config_path: Optional[str] = None):
    """Scans the Calibre library, generates KOReader hashes, and indexes them."""
    config: AppConfig = get_config(ctx, config_path)
    _, _, calibre, _ = init_components(config)

    if not calibre:
        console.print("[red]Calibre integration is disabled in configuration.[/red]")
        sys.exit(1)

    with console.status(f"Scanning Calibre library at {calibre.library_path}..."):
        count = calibre.scan_and_index_library(max_books=limit)

    console.print(f"[bold green]✔ Successfully indexed {count} book hashes in Calibre metadata.db![/bold green]")


@cli.command("status")
@click.option("--config", "-c", "config_path", help="Path to config.yaml file.")
@click.pass_context
def status(ctx, config_path: Optional[str] = None):
    """Displays tracked books and synchronization status."""
    config: AppConfig = get_config(ctx, config_path)
    db, _, _, _ = init_components(config)

    docs = db.get_all_tracked_documents()
    if not docs:
        console.print("[yellow]No books have been tracked yet. Start reading in KOReader or Kavita![/yellow]")
        return

    table = Table(title="Tracked Books & Reading Progress")
    table.add_column("Document Hash", style="cyan", no_wrap=True)
    table.add_column("Title", style="bold white")
    table.add_column("Author", style="magenta")
    table.add_column("Progress", style="green")
    table.add_column("Calibre ID", style="blue")
    table.add_column("Status", style="green")

    for d in docs:
        table.add_row(
            d["document"][:12] + "...",
            d["title"] or "Unknown",
            d["authors"] or "Unknown",
            f"{round(d['percentage'] * 100, 1)}%",
            str(d["calibre_book_id"]) if d["calibre_book_id"] else "-",
            d["last_sync_status"] or "synced",
        )

    console.print(table)


if __name__ == "__main__":
    cli()
