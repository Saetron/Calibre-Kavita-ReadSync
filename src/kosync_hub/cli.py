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
@click.pass_context
def cli(ctx, config_path: Optional[str]):
    """KOReader Multi-Sync Hub: Sync reading progress between Kavita and Calibre."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


@cli.command()
@click.pass_context
def serve(ctx):
    """Starts the KOReader sync server and background synchronizer."""
    config: AppConfig = ctx.obj["config"]
    db, kavita, calibre, sync = init_components(config)

    console.print(f"[bold cyan]Starting KOReader Multi-Sync Hub on {config.server.host}:{config.server.port}[/bold cyan]")
    app = create_app(config=config, db=db, synchronizer=sync)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


@cli.command("run")
@click.pass_context
def run(ctx):
    """Runs the bidirectional sync daemon in the foreground."""
    ctx.invoke(serve)


@cli.command("sync-now")
@click.pass_context
def sync_now(ctx):
    """Performs an immediate one-shot synchronization pass between Kavita and Calibre."""
    config: AppConfig = ctx.obj["config"]
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
@click.pass_context
def test_connections(ctx):
    """Verifies credentials and connectivity to Kavita and Calibre."""
    config: AppConfig = ctx.obj["config"]
    _, kavita, calibre, _ = init_components(config)

    async def _test():
        console.print("\n[bold]Testing Service Connections:[/bold]")
        if kavita:
            with console.status("Checking Kavita API..."):
                ok = await kavita.test_connection()
            if ok:
                console.print(f"[green]✔ Kavita:[/green] Successfully connected to {kavita.endpoint_url}")
            else:
                console.print(f"[red]✖ Kavita:[/red] Failed to connect/authenticate at {kavita.endpoint_url}")
        else:
            console.print("[yellow]– Kavita:[/yellow] Disabled in config")

        if calibre:
            with console.status("Checking Calibre Database..."):
                ok = await calibre.test_connection()
            if ok:
                console.print(f"[green]✔ Calibre DB:[/green] Successfully connected to {calibre.db_path}")
            else:
                console.print(f"[red]✖ Calibre DB:[/red] Failed to open database at {calibre.db_path}")
        else:
            console.print("[yellow]– Calibre:[/yellow] Disabled in config")

    asyncio.run(_test())


@cli.command("scan-calibre")
@click.option("--limit", "-l", default=None, type=int, help="Limit number of books to scan.")
@click.pass_context
def scan_calibre(ctx, limit: Optional[int]):
    """Scans the Calibre library, generates KOReader hashes, and indexes them."""
    config: AppConfig = ctx.obj["config"]
    _, _, calibre, _ = init_components(config)

    if not calibre:
        console.print("[red]Calibre integration is disabled in configuration.[/red]")
        sys.exit(1)

    with console.status(f"Scanning Calibre library at {calibre.library_path}..."):
        count = calibre.scan_and_index_library(max_books=limit)

    console.print(f"[bold green]✔ Successfully indexed {count} book hashes in Calibre metadata.db![/bold green]")


@cli.command("status")
@click.pass_context
def status(ctx):
    """Displays tracked books and synchronization status."""
    config: AppConfig = ctx.obj["config"]
    db, _, _, _ = init_components(config)

    docs = db.get_all_tracked_documents()
    if not docs:
        console.print("[yellow]No books have been tracked yet. Point your KOReader device to this server or read a book![/yellow]")
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
