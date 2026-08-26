import re

from homeconnect import help as help_module
from homeconnect.present import display_width


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_every_section_has_entries():
    assert help_module.SECTIONS
    for section in help_module.SECTIONS:
        assert section.name
        assert section.entries, f"{section.name} has no entries"


def test_reference_mentions_each_command():
    text = strip_ansi(help_module.render_reference())
    for section in help_module.SECTIONS:
        for command, _description in section.entries:
            assert command in text


def test_box_rules_are_all_the_same_display_width():
    """len() on box-drawing characters is what makes these ragged."""
    lines = strip_ansi(help_module.render_reference()).splitlines()
    rules = [line for line in lines if line.startswith(("╔", "╚", "║"))]
    assert rules, "expected a box"
    widths = {display_width(line) for line in rules}
    assert len(widths) == 1, f"box edges are ragged: {widths}"


def test_long_descriptions_wrap_under_the_description_column():
    section = help_module.Section(
        name="TEST", blurb="", entries=[("cmd", "word " * 40)]
    )
    lines = [
        line
        for line in strip_ansi(help_module.render_section(section, width=60)).splitlines()
        if line.strip()
    ]
    entry_lines = [line for line in lines if "word" in line]
    assert len(entry_lines) > 1, "expected the description to wrap"
    indents = {len(line) - len(line.lstrip()) for line in entry_lines[1:]}
    first_description_column = entry_lines[0].index("word")
    assert indents == {first_description_column}


def test_reference_states_what_the_api_cannot_provide():
    """Recorded in the help so nobody goes hunting for it later."""
    text = strip_ansi(help_module.render_reference()).lower()
    assert "energy" in text
    assert "read-only" in text
