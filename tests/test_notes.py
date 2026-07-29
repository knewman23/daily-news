import pytest

from src import notes


DAY = """---
date: 2026-07-28
generated: 2026-07-28T11:00:04+00:00
tags: [politics, markets]
sources: ["@aaronparnas", "@carolinegleich"]
post_count: 4
transcribed_count: 4
incomplete: false
---

# July 28, 2026

## Senate passes the spending bill
tags: politics
sources: @aaronparnas, @total.hipocrisy

The chamber cleared the measure after a weekend of negotiation.

## Nvidia earnings beat estimates
tags: markets, tech
sources: @carolinegleich

Revenue came in ahead of guidance.

## My Notes
<!-- notes:start -->
<!-- notes:end -->
"""


@pytest.fixture
def day(tmp_path):
    p = tmp_path / "2026-07-28.md"
    p.write_text(DAY, encoding="utf-8")
    return p


def outside(text):
    """Everything before the start marker and after the end marker."""
    head, rest = text.split(notes.START, 1)
    _body, tail = rest.split(notes.END, 1)
    return head, tail


# --- the round trip that protects the generated summary --------------------


def test_writing_notes_changes_nothing_outside_the_markers(day):
    before = day.read_text(encoding="utf-8")

    notes.write_notes(day, "This one worries me.")

    after = day.read_text(encoding="utf-8")
    assert outside(after) == outside(before)
    assert notes.read_notes(day) == "This one worries me."


def test_summary_body_survives_many_writes(day):
    for i in range(10):
        notes.write_notes(day, f"pass {i}")

    after = day.read_text(encoding="utf-8")
    assert "Senate passes the spending bill" in after
    assert "Revenue came in ahead of guidance." in after
    assert after.count(notes.START) == 1
    assert after.count(notes.END) == 1
    assert notes.read_notes(day) == "pass 9"


# --- content edge cases ---------------------------------------------------


def test_empty_notes_clears_the_block(day):
    notes.write_notes(day, "Something.")
    notes.write_notes(day, "")

    assert notes.read_notes(day) == ""
    assert "Something." not in day.read_text(encoding="utf-8")


def test_read_notes_on_untouched_file_returns_empty(day):
    assert notes.read_notes(day) == ""


def test_notes_containing_frontmatter_fence_do_not_corrupt_the_header(day):
    notes.write_notes(day, "---\nnot: frontmatter\n---")

    text = day.read_text(encoding="utf-8")
    assert text.startswith("---\ndate: 2026-07-28\n")
    assert notes.read_notes(day) == "---\nnot: frontmatter\n---"


def test_notes_containing_a_heading_round_trip(day):
    notes.write_notes(day, "## My own heading\nreaction text")
    assert notes.read_notes(day) == "## My own heading\nreaction text"


def test_multiline_notes_with_ragged_whitespace(day):
    notes.write_notes(day, "\n\n  line one  \nline two\n\n\n")
    assert notes.read_notes(day) == "line one  \nline two"


def test_identical_writes_are_byte_idempotent(day):
    notes.write_notes(day, "same text")
    once = day.read_text(encoding="utf-8")
    notes.write_notes(day, "same text")
    assert day.read_text(encoding="utf-8") == once


def test_read_then_write_is_stable(day):
    notes.write_notes(day, "original")
    first = day.read_text(encoding="utf-8")

    notes.write_notes(day, notes.read_notes(day))
    assert day.read_text(encoding="utf-8") == first


@pytest.mark.parametrize("payload", [
    f"sneaky {notes.START}",
    f"sneaky {notes.END}",
    f"{notes.START}{notes.END}",
])
def test_notes_may_not_contain_the_markers(day, payload):
    before = day.read_text(encoding="utf-8")
    with pytest.raises(notes.NotesMarkerError):
        notes.write_notes(day, payload)
    assert day.read_text(encoding="utf-8") == before


# --- malformed files ------------------------------------------------------


@pytest.mark.parametrize("text", [
    DAY.replace(notes.START + "\n", ""),
    DAY.replace(notes.END + "\n", ""),
    DAY.replace(notes.START, notes.START + "\n" + notes.START),
    DAY.replace(notes.END, notes.END + "\n" + notes.END),
    DAY.replace(
        notes.START + "\n" + notes.END,
        notes.END + "\n" + notes.START,
    ),
    "# no markers at all\n",
])
def test_malformed_markers_raise(tmp_path, text):
    p = tmp_path / "broken.md"
    p.write_text(text, encoding="utf-8")

    with pytest.raises(notes.NotesMarkerError):
        notes.read_notes(p)
    with pytest.raises(notes.NotesMarkerError):
        notes.write_notes(p, "anything")


def test_malformed_file_is_not_modified_by_a_failed_write(tmp_path):
    p = tmp_path / "broken.md"
    p.write_text("# no markers at all\n", encoding="utf-8")

    with pytest.raises(notes.NotesMarkerError):
        notes.write_notes(p, "anything")
    assert p.read_text(encoding="utf-8") == "# no markers at all\n"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        notes.read_notes(tmp_path / "absent.md")


def test_no_temp_file_left_behind(day):
    notes.write_notes(day, "text")
    assert list(day.parent.glob("*.tmp")) == []
