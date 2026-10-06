"""Command-line interface for the on-call agent.

Two modes:

* **Interactive** (``oncall`` or ``oncall chat``) — a REPL with slash commands.
* **Non-interactive** (``oncall ask "..."`` or piped stdin) — one-shot, with
  ``--json`` so the agent can be scripted or run in CI.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from . import data
from .agent import AgentResponse, OncallSession
from .config import ConfigError, load_settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="On-call incident response agent — incidents, deploys, telemetry, runbooks.",
)
console = Console()
err_console = Console(stderr=True)

BANNER = """[bold cyan]On-call Agent[/bold cyan] [dim]· Meridian commerce platform[/dim]
[dim]Incident triage, change correlation, telemetry, and runbooks.[/dim]"""

EXAMPLES = [
    "INC-4821 just paged me. What's going on and what should I do first?",
    "Why is checkout-web slow on INC-4830? Nothing deployed.",
    "Has the ledger-writer pool exhaustion happened before, and how was it fixed?",
    "Walk me through recovering session-store memory pressure using RB-031.",
    "Which team is slowest to resolve incidents, and is it detection or repair?",
    "Who owns index-builder and who should I page for INC-4826?",
    "Draft the fix for INC-4821 and tell me whether it passes its tests.",
    "Is rolling back dep-7c22e18 safe to ship for INC-4826?",
]


def _print_sources(sources: list[dict[str, Any]]) -> None:
    if not sources:
        return
    table = Table(title="External sources", show_lines=False, title_justify="left", header_style="bold")
    table.add_column("#", width=3, style="cyan")
    table.add_column("Title", overflow="fold")
    table.add_column("Published", width=12, style="dim")
    for source in sources:
        table.add_row(
            str(source.get("n", "")),
            f"{source.get('title', '')}\n[dim blue]{source.get('url', '')}[/dim blue]",
            source.get("published_date") or "—",
        )
    console.print(table)


def _print_response(response: AgentResponse, show_trace: bool) -> None:
    console.print()
    console.print(Markdown(response.answer or "_(no answer produced)_"))
    console.print()
    _print_sources(response.sources)
    if show_trace:
        context = response.context
        bits = [f"{k}={v}" for k, v in context.items() if k in {"incident_id", "team", "severity", "domain"}]
        if bits:
            console.print(f"[dim]context: {'  '.join(bits)}[/dim]")
        if response.tool_calls:
            console.print(
                f"[dim]tools: {' → '.join(response.tool_calls)} "
                f"({response.iterations} model turn(s))[/dim]"
            )


def _print_active_incidents() -> None:
    table = Table(title="Active incidents", title_justify="left", header_style="bold")
    for column, width in (("ID", 10), ("Sev", 5), ("Service", 18), ("Team", 14), ("Title", None)):
        table.add_column(column, width=width, overflow="fold")
    for incident in data.find_incidents(status="ACTIVE", limit=25):
        table.add_row(
            incident["incident_id"],
            incident["severity"],
            incident["service"],
            incident["team"],
            incident["title"],
        )
    console.print(table)


def _make_session(mode: str) -> OncallSession:
    try:
        settings = load_settings()
    except ConfigError as exc:
        err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2)

    session = OncallSession(settings=settings, mode=mode)
    if not session.tracing_enabled:
        err_console.print(
            "[yellow]Warning:[/yellow] BRAINTRUST_API_KEY not set — running without tracing."
        )
    if not settings.web_search_enabled:
        err_console.print(
            "[yellow]Warning:[/yellow] EXA_API_KEY not set — external research is disabled."
        )
    return session


@app.command()
def ask(
    question: str = typer.Argument(None, help="The question. Reads stdin if omitted."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of formatted text."),
    show_trace: bool = typer.Option(False, "--show-trace", help="Print the tool call sequence."),
) -> None:
    """Ask one question and exit (non-interactive)."""
    if not question:
        if sys.stdin.isatty():
            err_console.print("[bold red]Error:[/bold red] provide a question or pipe one on stdin.")
            raise typer.Exit(code=2)
        question = sys.stdin.read().strip()

    if not question:
        err_console.print("[bold red]Error:[/bold red] empty question.")
        raise typer.Exit(code=2)

    session = _make_session(mode="oneshot")

    if as_json:
        # Keep stdout pure JSON so the command composes with jq et al.
        logging.disable(logging.CRITICAL)
        response = session.ask(question)
        print(json.dumps(response.to_dict(), indent=2))
        return

    with console.status("[dim]Investigating…[/dim]", spinner="dots"):
        response = session.ask(question)
    _print_response(response, show_trace)


@app.command()
def chat(
    show_trace: bool = typer.Option(True, "--show-trace/--no-show-trace", help="Print tool calls."),
) -> None:
    """Start the interactive on-call session (default)."""
    session = _make_session(mode="interactive")

    console.print(Panel(BANNER, border_style="cyan"))
    console.print(
        f"[dim]project: {session.settings.braintrust_project}  ·  "
        f"model: {session.settings.model} / {session.settings.reasoning_model}  ·  "
        f"prompt: {session.settings.prompt_source}  ·  "
        f"tracing: {'on' if session.tracing_enabled else 'off'}[/dim]"
    )
    console.print(f"[dim]tags: {'  '.join(session.settings.trace_tags(session.mode))}[/dim]")
    console.print("[dim]/incidents  /examples  /reset  /trace  /exit[/dim]\n")

    while True:
        try:
            question = console.input("[bold cyan]›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/dim]")
            return

        if not question:
            continue

        if question.startswith("/"):
            command = question.lower()
            if command in {"/exit", "/quit", "/q"}:
                console.print("[dim]Bye.[/dim]")
                return
            if command == "/reset":
                session.reset()
                console.print("[dim]Conversation cleared.[/dim]\n")
                continue
            if command == "/trace":
                show_trace = not show_trace
                console.print(f"[dim]Tool trace {'on' if show_trace else 'off'}.[/dim]\n")
                continue
            if command == "/incidents":
                _print_active_incidents()
                console.print()
                continue
            if command == "/examples":
                for example in EXAMPLES:
                    console.print(f"  [dim]•[/dim] {example}")
                console.print()
                continue
            console.print(
                "[yellow]Unknown command.[/yellow] /incidents /examples /reset /trace /exit\n"
            )
            continue

        try:
            with console.status("[dim]Investigating…[/dim]", spinner="dots"):
                response = session.ask(question)
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/yellow]\n")
            continue
        except Exception as exc:
            err_console.print(f"[bold red]Error:[/bold red] {exc}")
            continue

        _print_response(response, show_trace)
        console.print()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Run the interactive session when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        chat()


if __name__ == "__main__":
    app()
