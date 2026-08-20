from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from status.collectors import run_collect
from status.config import SKILLS_DIR, get_settings
from status.db import get_session
from status.skills.client import SkillClient
from status.skills.drafter import draft_and_persist, load_fixture, run_drafter
from status.skills.synthesizer import run_synthesizer

app = typer.Typer(no_args_is_help=True, help="Weekly status pipeline CLI")
skills_app = typer.Typer(no_args_is_help=True, help="Manage Claude Agent Skills")
slack_app = typer.Typer(no_args_is_help=True, help="Slack bot for draft review")
app.add_typer(skills_app, name="skills")
app.add_typer(slack_app, name="slack")

console = Console()


def _parse_week(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _dry_run_flag(dry_run: bool) -> None:
    if dry_run:
        console.print("[yellow]dry-run: no external calls or persistence[/]")


@app.command()
def collect(
    person: Annotated[str, typer.Option("--person", "-p", help="Person ID")],
    week: Annotated[str, typer.Option("--week", "-w", help="Week ending Friday (YYYY-MM-DD)")],
    save_fixture: Annotated[
        Optional[Path], typer.Option("--save-fixture", help="Write payload JSON to this path")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip external API calls")] = False,
    jira_account: Annotated[
        Optional[str], typer.Option("--jira-account", help="Override Jira account id")
    ] = None,
    github_login: Annotated[
        Optional[str], typer.Option("--github-login", help="Override GitHub login")
    ] = None,
) -> None:
    """Collect Jira and GitHub activity for one person and one week."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    payload = run_collect(
        person,
        week_ending,
        save_fixture=save_fixture,
        dry_run=dry_run,
        jira_account_id=jira_account,
        github_login=github_login,
    )
    console.print_json(json.dumps(payload, indent=2))


@app.command()
def draft(
    fixture: Annotated[
        Optional[Path], typer.Option("--fixture", "-f", help="Collector payload JSON")
    ] = None,
    person: Annotated[
        Optional[str], typer.Option("--person", "-p", help="Person ID (collects live data)")
    ] = None,
    week: Annotated[
        Optional[str], typer.Option("--week", "-w", help="Week ending Friday (YYYY-MM-DD)")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip skill invocation")] = False,
    no_persist: Annotated[
        bool, typer.Option("--no-persist", help="Do not write draft rows to Postgres")
    ] = False,
) -> None:
    """Invoke the drafter skill on a collector payload and persist draft rows."""
    _dry_run_flag(dry_run)

    if fixture is not None:
        payload = load_fixture(fixture)
    elif person and week:
        payload = run_collect(person, _parse_week(week))
    else:
        console.print("[red]Provide --fixture or both --person and --week[/]")
        raise typer.Exit(1)

    if dry_run or no_persist:
        result = run_drafter(payload, dry_run=dry_run)
        console.print_json(result.model_dump_json(indent=2))
        return

    run_result = draft_and_persist(payload, dry_run=False, persist=True)

    output = run_result.draft.model_dump()
    output["prompt_version"] = run_result.prompt_version
    output["persisted_entry_ids"] = run_result.persisted_entry_ids
    output["superseded_count"] = run_result.superseded_count
    console.print_json(json.dumps(output, indent=2))


@app.command()
def send(
    person: Annotated[str, typer.Option("--person", "-p")],
    week: Annotated[str, typer.Option("--week", "-w")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Send a draft review DM via Slack."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    settings = get_settings()

    if dry_run:
        console.print(
            f"[dim]would send draft review: person={person} week={week_ending}[/]"
        )
        return

    if not settings.slack_bot_token:
        console.print("[red]SLACK_BOT_TOKEN not set[/]")
        raise typer.Exit(1)

    from status.slack.send import SlackSendError, send_draft_review

    try:
        result = send_draft_review(person, week_ending, bot_token=settings.slack_bot_token)
    except SlackSendError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    console.print_json(json.dumps(result, indent=2))


@slack_app.command("run")
def slack_run() -> None:
    """Run the Slack Socket Mode handler for draft review."""
    from status.slack.app import SlackAppError, run_socket_mode

    try:
        run_socket_mode()
    except SlackAppError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc


@app.command(name="report")
def report_cmd(
    week: Annotated[str, typer.Option("--week", "-w")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = True,
    output: Annotated[
        Optional[Path], typer.Option("--output", "-o", help="Write markdown report to file")
    ] = None,
) -> None:
    """Synthesize the management report for a week."""
    _dry_run_flag(dry_run)
    week_ending = _parse_week(week)
    with get_session() as session:
        result = run_synthesizer(session, week_ending, dry_run=dry_run)

    if output:
        output.write_text(result.markdown)
        console.print(f"Wrote report to {output}")
    else:
        console.print(result.markdown)


@skills_app.command("list")
def skills_list() -> None:
    """List custom skills in the workspace."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set[/]")
        raise typer.Exit(1)

    client = SkillClient(api_key=settings.anthropic_api_key)
    skills = client.list_custom()
    table = Table("ID", "Name", "Created")
    for skill in skills:
        table.add_row(skill.id, getattr(skill, "display_title", ""), str(getattr(skill, "created_at", "")))
    console.print(table)


@skills_app.command("publish")
def skills_publish(
    skill: Annotated[str, typer.Option("--skill", help="drafter or synthesizer")],
) -> None:
    """Publish a new version of a skill from the local skills/ directory."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set[/]")
        raise typer.Exit(1)

    skill_map = {
        "drafter": ("weekly-status-drafter", settings.drafter_skill_id),
        "synthesizer": ("weekly-status-synthesizer", settings.synthesizer_skill_id),
    }
    if skill not in skill_map:
        console.print(f"[red]Unknown skill: {skill}. Use drafter or synthesizer.[/]")
        raise typer.Exit(1)

    dir_name, skill_id = skill_map[skill]
    skill_dir = SKILLS_DIR / dir_name
    if not skill_dir.exists():
        console.print(f"[red]Skill directory not found: {skill_dir}[/]")
        raise typer.Exit(1)

    client = SkillClient(api_key=settings.anthropic_api_key)
    if skill_id:
        version = client.publish_version(skill_id, skill_dir)
        console.print(f"Published {dir_name} version {version}")
    else:
        new_id = client.upload(skill_dir, display_name=dir_name)
        console.print(f"Created {dir_name} with id {new_id}")
        console.print(f"Set {skill.upper()}_SKILL_ID={new_id} in your environment")


if __name__ == "__main__":
    app()
