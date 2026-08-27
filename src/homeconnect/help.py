"""Coloured, boxed help output.

Matches the house style of the sibling `hue-control` tool (cyan bold
headings, green command names, dim white descriptions, box-drawing rules),
with two deliberate departures documented in the task brief: continuation
lines wrap under the description column rather than back to column two, and
all width arithmetic uses `display_width()` rather than `len()`, since
box-drawing characters and other non-ASCII symbols can occupy two terminal
columns while counting as a single character.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

import click

from .present import display_width


@dataclass(frozen=True)
class Section:
    """One block of the quick-reference help: a heading and its commands."""

    name: str
    blurb: str
    entries: list[tuple[str, str]]


SECTIONS: list[Section] = [
    Section(
        name="STATUS",
        blurb="What the appliances are doing right now.",
        entries=[
            ("homeconnect", "One-line verdict for every appliance."),
            ("homeconnect --verbose", "Every field, with raw API key names."),
            ("homeconnect --json", "Machine-readable output."),
            ("homeconnect -a <name>", "Filter to appliances matching this name or type."),
        ],
    ),
    Section(
        name="HISTORY",
        blurb="What the recorder has observed over time.",
        entries=[
            ("homeconnect history", "Report on cycles, alerts and coverage gaps."),
            ("homeconnect history -x", "The complete alert record, with intervals."),
            ("homeconnect history --json", "Machine-readable history."),
        ],
    ),
    Section(
        name="RECORDER",
        blurb="The background listener that builds the history.",
        entries=[
            ("launchd/install.sh", "Install and start the recorder at login."),
            ("launchd/uninstall.sh", "Stop and remove the recorder."),
            ("homeconnect-recorder", "Run the recorder in the foreground, for debugging."),
        ],
    ),
    Section(
        name="SETUP",
        blurb="One-off, before first use.",
        entries=[
            ("homeconnect auth", "Authorise this machine (device-flow consent)."),
        ],
    ),
]

_CLOSING_NOTE = (
    "This tool is read-only by construction: it holds a read-only OAuth scope, "
    "and no write verb exists in the codebase. Energy use, water use and cycle "
    "counts are not available from the Home Connect API at all; the recorder's "
    "history is the closest available substitute, built by observing state "
    "changes over time."
)


def _wrap_entry(command: str, description: str, command_column: int, description_column: int, width: int) -> list[str]:
    """Render one (command, description) row, wrapped under the description column."""
    wrap_width = max(width - description_column, 20)
    wrapped = textwrap.wrap(description, width=wrap_width) or [""]

    lines = []
    padding = description_column - command_column - display_width(command)
    first = (
        " " * command_column
        + click.style(command, fg="green")
        + " " * padding
        + click.style(wrapped[0], fg="white", dim=True)
    )
    lines.append(first)
    for extra in wrapped[1:]:
        lines.append(" " * description_column + click.style(extra, fg="white", dim=True))
    return lines


def render_section(section: Section, width: int = 78) -> str:
    """Render one section as a coloured, wrapped block of text."""
    command_column = 2
    longest_command = max(display_width(command) for command, _ in section.entries)
    description_column = command_column + longest_command + 2

    lines = [click.style(section.name, fg="cyan", bold=True)]
    if section.blurb:
        lines.append(click.style(f"  {section.blurb}", fg="white", dim=True))
    for command, description in section.entries:
        lines.extend(_wrap_entry(command, description, command_column, description_column, width))
    return "\n".join(lines)


def render_reference(width: int = 78) -> str:
    """Render the full quick-reference help, boxed heading and all sections."""
    title = "Home Connect - Quick Reference"
    inner_width = max(width - 2, display_width(title) + 2)

    top = "╔" + "═" * inner_width + "╗"
    bottom = "╚" + "═" * inner_width + "╝"
    padding = inner_width - display_width(title)
    left_pad = padding // 2
    right_pad = padding - left_pad
    middle = "║" + " " * left_pad + title + " " * right_pad + "║"

    lines = [
        click.style(top, fg="cyan", bold=True),
        click.style(middle, fg="cyan", bold=True),
        click.style(bottom, fg="cyan", bold=True),
        "",
    ]

    for section in SECTIONS:
        lines.append(render_section(section, width=width))
        lines.append("")

    lines.append(click.style("NOTE", fg="cyan", bold=True))
    for wrapped_line in textwrap.wrap(_CLOSING_NOTE, width=width - 2):
        lines.append(click.style(f"  {wrapped_line}", fg="white", dim=True))

    return "\n".join(lines)


class ColouredGroup(click.Group):
    """A `click.Group` with coloured help output and did-you-mean suggestions."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError as exc:
            if "No such command" not in str(exc):
                raise
            attempted = args[0] if args else ""
            suggestions = self._suggest(ctx, attempted)
            message = f"No such command '{attempted}'."
            if suggestions:
                message += "\n\n" + click.style("Did you mean one of these?", fg="yellow")
                for suggestion in suggestions:
                    message += "\n" + click.style(f"  {suggestion}", fg="green")
            raise click.UsageError(message) from exc

    def _suggest(self, ctx, attempted: str, limit: int = 3) -> list[str]:
        if not attempted:
            return []
        attempted_lower = attempted.lower()
        scored = []
        for name in self.list_commands(ctx):
            command = self.get_command(ctx, name)
            if command is None or command.hidden:
                continue
            score = self._similarity(attempted_lower, name.lower())
            if score > 0:
                scored.append((score, name))
        scored.sort(reverse=True, key=lambda item: item[0])
        return [name for _score, name in scored[:limit]]

    @staticmethod
    def _similarity(left: str, right: str) -> int:
        """A simple similarity score: exact, prefix, substring, then shared characters in order."""
        if left == right:
            return 100
        if right.startswith(left) or left.startswith(right):
            return 80
        if left in right or right in left:
            return 60

        matches = 0
        cursor = 0
        for char in left:
            while cursor < len(right):
                if right[cursor] == char:
                    matches += 1
                    cursor += 1
                    break
                cursor += 1

        if matches == 0:
            return 0
        score = int((matches / max(len(left), len(right))) * 50)
        return score if score > 20 else 0

    def format_usage(self, ctx, formatter):
        formatter.write_paragraph()
        formatter.write_text(
            click.style("Usage: ", fg="cyan", bold=True)
            + click.style(f"{ctx.command_path} [OPTIONS] COMMAND [ARGS]...", fg="white")
        )

    def format_options(self, ctx, formatter):
        records = []
        for param in self.get_params(ctx):
            record = param.get_help_record(ctx)
            if record is not None:
                records.append(record)

        if not records:
            return

        formatter.write_paragraph()
        formatter.write(click.style("Options:", fg="yellow", bold=True) + "\n")
        # Written with formatter.write() (raw, unwrapped) rather than
        # write_text(): write_text runs Click's own textwrap over the line,
        # which counts embedded ANSI escape codes as display characters and
        # wraps in the wrong place, breaking the padded column entirely.
        longest = max(display_width(name) for name, _ in records)
        for name, description in records:
            padding = longest - display_width(name) + 2
            formatter.write(
                "  " + click.style(name, fg="green") + " " * padding
                + click.style(description, fg="white", dim=True) + "\n"
            )

        # Click's own MultiCommand.format_options chains into format_commands
        # after writing the options; not doing so here made the whole
        # "Commands:" section vanish from `--help`.
        self.format_commands(ctx, formatter)

    def format_commands(self, ctx, formatter):
        commands = []
        for name in self.list_commands(ctx):
            command = self.get_command(ctx, name)
            if command is None or command.hidden:
                continue
            commands.append((name, command.get_short_help_str(limit=500)))

        if not commands:
            return

        formatter.write_paragraph()
        formatter.write(click.style("Commands:", fg="yellow", bold=True) + "\n")

        # Raw formatter.write(), not write_text(): see the comment in
        # format_options() above — write_text's textwrap miscounts ANSI
        # escape codes and wraps padded columns in the wrong place.
        longest = max(display_width(name) for name, _ in commands)
        for name, description in commands:
            padding = longest - display_width(name) + 2
            formatter.write(
                "  " + click.style(name, fg="green") + " " * padding
                + click.style(description, fg="white", dim=True) + "\n"
            )


@click.command(name="help")
def help_command() -> None:
    """Show a coloured quick reference for every command."""
    click.echo(render_reference())
