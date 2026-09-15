# -*- coding: utf-8 -*-
"""Tests for the locked comparison-report format and its difference engine.

The rules these lock down were derived from a full production comparison: they
are the reason two textually different definitions can still be the same object.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "compare_release"
sys.path.insert(0, str(SCRIPTS))

import report_format as fmt  # noqa: E402
from sources import find_object_definitions, load_source  # noqa: E402


# --------------------------------------------------------------------------- #
# Normalisation rules
# --------------------------------------------------------------------------- #

def test_create_or_alter_is_not_a_difference():
    """SQL Server stores 'CREATE OR ALTER X' as 'CREATE    X'. Same object."""
    assert fmt.norm_key("CREATE OR ALTER PROC dbo.X") == fmt.norm_key("CREATE    PROC dbo.X")


def test_proc_and_procedure_are_the_same_keyword():
    assert fmt.norm_key("CREATE PROC dbo.X") == fmt.norm_key("CREATE PROCEDURE dbo.X")


def test_indentation_and_trailing_space_are_ignored():
    assert fmt.norm_key("\t  SELECT   1;   ") == fmt.norm_key("SELECT 1;")


def test_real_text_change_is_still_a_difference():
    assert fmt.norm_key("SELECT 1;") != fmt.norm_key("SELECT 2;")


# --------------------------------------------------------------------------- #
# Difference engine
# --------------------------------------------------------------------------- #

def test_blank_lines_are_excluded_but_counted():
    left = "CREATE PROC dbo.X\nAS\nSELECT 1;"
    right = "CREATE PROC dbo.X\n\n\nAS\n\nSELECT 1;"
    rows, st = fmt.build_diff(left, right)
    assert st.is_identical, "blank lines alone must not register as a change"
    assert st.left_blank == 0
    assert st.right_blank == 3
    assert st.left_content == st.right_content == 3
    assert len(rows) == 3


def test_every_content_line_appears_exactly_once():
    left = "\n".join(f"line {i}" for i in range(20))
    right = "\n".join(f"line {i}" for i in range(0, 20, 2))
    rows, st = fmt.build_diff(left, right)
    emitted_left = sum(1 for k, ln, lt, rn, rt in rows if lt is not None)
    emitted_right = sum(1 for k, ln, lt, rn, rt in rows if rt is not None)
    assert emitted_left == st.left_content
    assert emitted_right == st.right_content


def test_added_changed_removed_are_classified():
    left = "A\nB\nC"
    right = "A\nB2\nC\nD"
    rows, st = fmt.build_diff(left, right)
    assert st.changed == 1
    assert st.added == 1
    assert st.removed == 0
    assert not st.is_identical


def test_spacing_only_difference_is_its_own_category():
    rows, st = fmt.build_diff("SELECT 1;", "   SELECT    1;")
    assert st.ws == 1 and st.same == 0
    assert st.is_identical, "spacing-only must not count as changed/added/removed"


def test_logical_sha_ignores_storage_artifacts():
    a = "CREATE OR ALTER PROC dbo.X\nAS\n  SELECT 1;\n"
    b = "CREATE    PROC dbo.X\n\nAS\nSELECT   1;"
    assert fmt.logical_sha(a) == fmt.logical_sha(b)


# --------------------------------------------------------------------------- #
# Sources - one loader per comparison side
# --------------------------------------------------------------------------- #

def test_consolidated_file_last_definition_wins():
    text = (
        "CREATE PROC dbo.A\nAS\nSELECT 1;\nGO\n"
        "CREATE PROC dbo.A\nAS\nSELECT 2;\nGO\n"
    )
    objs = find_object_definitions(text)
    assert set(objs) == {"A"}
    assert "SELECT 2;" in objs["A"]


def test_split_line_create_or_alter_is_found():
    text = "CREATE\n OR ALTER PROCEDURE dbo.B\nAS\nSELECT 1;\nGO\n"
    assert "B" in find_object_definitions(text)


def test_load_source_accepts_mcp_dump_envelope(tmp_path: Path):
    p = tmp_path / "dump.json"
    p.write_text(json.dumps({"result": {"columns": ["proc_name", "def"],
                                        "rows": [["SP_X", "CREATE PROC dbo.SP_X AS SELECT 1;"]]}}),
                 encoding="utf-8")
    objs, desc = load_source(f"dump:{p}")
    assert objs["SP_X"].startswith("CREATE PROC")
    assert desc == "dump.json"


def test_load_source_accepts_plain_mapping(tmp_path: Path):
    p = tmp_path / "plain.json"
    p.write_text(json.dumps({"SP_Y": "CREATE PROC dbo.SP_Y AS SELECT 1;"}), encoding="utf-8")
    objs, _ = load_source(f"dump:{p}")
    assert "SP_Y" in objs


def test_load_source_reads_a_directory(tmp_path: Path):
    (tmp_path / "SP_Z.sql").write_text("CREATE PROC dbo.SP_Z AS SELECT 1;", encoding="utf-8")
    objs, _ = load_source(f"dir:{tmp_path}")
    assert "SP_Z" in objs


def test_windows_drive_letter_is_not_mistaken_for_a_kind(tmp_path: Path):
    p = tmp_path / "rel.sql"
    p.write_text("CREATE PROC dbo.SP_W AS SELECT 1;\nGO\n", encoding="utf-8")
    objs, _ = load_source(str(p))          # bare path, no kind prefix
    assert "SP_W" in objs


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _sample_report() -> str:
    rows, st = fmt.build_diff("CREATE PROC dbo.X\nAS\nSELECT 1;",
                              "CREATE PROC dbo.X\nAS\nSELECT 2;")
    rep = fmt.Report(title="T", eyebrow="E", headline="H", standfirst="S",
                     left_label="Left env", right_label="Right env",
                     meta=[("Left", "a"), ("Right", "b")])
    rep.lede(["one"]).tiles([("chg", "1", "differ")])
    rep.findings([("high", "1.1", "Title", "Body")])
    rep.reading_guide()
    rep.section("diffs", "Line-by-line comparison")
    rep.object_diff("o-X", "X", rows, st)
    rep.method(["p"], [("k", "v")])
    return rep.render()


def test_report_renders_both_side_labels_and_diff_rows():
    html = _sample_report()
    assert "Left env" in html and "Right env" in html
    assert 'class="diff"' in html
    assert 'class="r-chg"' in html


def test_report_defines_every_colour_token_in_bare_root():
    """A token defined only inside a media query renders unreadable in the
    un-stamped (system-theme) state."""
    html = fmt.CSS
    bare = html.split(":root{", 1)[1].split("}", 1)[0]
    for token in ("--ground", "--panel", "--ink", "--accent", "--add-bg", "--del-bg",
                  "--chg", "--new", "--ok", "--crit"):
        assert token in bare, f"{token} missing from the bare :root palette"


def test_standalone_render_is_a_full_document():
    rows, st = fmt.build_diff("A", "B")
    rep = fmt.Report("T", "E", "H", "S", "L", "R")
    rep.section("s", "S")
    rep.object_diff("o-A", "A", rows, st)
    out = rep.render(standalone=True)
    assert out.lstrip().lower().startswith("<!doctype html>")
    assert out.rstrip().endswith("</html>")


def test_embedded_render_has_no_document_scaffolding():
    """Artifact hosting supplies <!doctype>/<head>/<body> itself."""
    out = _sample_report()
    assert "<!doctype" not in out.lower()
    assert "<body>" not in out.lower()


def test_html_in_object_text_is_escaped():
    rows, st = fmt.build_diff("SELECT '<script>' AS x;", "SELECT '<b>' AS x;")
    rep = fmt.Report("T", "E", "H", "S", "L", "R")
    rep.section("s", "S")
    rep.object_diff("o-X", "X", rows, st)
    out = rep.render()
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_format_version_is_pinned():
    assert fmt.FORMAT_VERSION == "1.0"
