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
    assert fmt.FORMAT_VERSION == "1.2"


# --------------------------------------------------------------------------- #
# Same-shape-before-comparing (v1.1) - the false-positive class reported by the
# team: production stores a header split over several lines, under a leftover
# release separator rule, while the release file has it on one line.
# --------------------------------------------------------------------------- #

PROD_SHAPE = (
    "-" * 60 + "\n"
    "\n"
    "CREATE\n"
    "\n"
    "\n"
    " PROCEDURE [dbo].[SP_X] (\n"
    "\t@Id INT\n"
    "\t)\n"
    "AS\n"
    "BEGIN\n"
    "\tSELECT 1;\n"
    "END"
)

FILE_SHAPE = (
    "CREATE OR ALTER PROCEDURE [dbo].[SP_X] (\n"
    "\t@Id INT\n"
    "\t)\n"
    "AS\n"
    "BEGIN\n"
    "\tSELECT 1;\n"
    "END"
)


def test_split_header_and_separator_residue_are_not_differences():
    rows, st = fmt.build_diff(PROD_SHAPE, FILE_SHAPE)
    assert st.is_identical, (
        f"formatting alone registered as a change: "
        f"changed={st.changed} added={st.added} removed={st.removed}"
    )


def test_separator_residue_is_counted_not_silently_dropped():
    _, st = fmt.build_diff(PROD_SHAPE, FILE_SHAPE)
    assert st.left_preamble == 1
    assert st.right_preamble == 0


def test_merged_header_keeps_the_real_line_number():
    rows, _ = fmt.build_diff(PROD_SHAPE, FILE_SHAPE)
    kind, ln, lt, rn, rt = rows[0]
    assert ln == 3, "left line number must point at the CREATE, not the merged text"
    assert rn == 1
    assert "PROCEDURE" in lt and "SP_X" in lt


def test_a_real_header_change_is_still_reported():
    changed = FILE_SHAPE.replace("@Id INT", "@Id BIGINT")
    _, st = fmt.build_diff(PROD_SHAPE, changed)
    assert not st.is_identical
    assert st.changed == 1


def test_a_renamed_object_is_still_reported():
    renamed = FILE_SHAPE.replace("SP_X", "SP_Y")
    _, st = fmt.build_diff(PROD_SHAPE, renamed)
    assert not st.is_identical


def test_real_comment_above_header_is_kept_when_not_residue():
    """A separator rule is residue; a sentence is documentation - keep it."""
    left = "-- owner: reporting team\nCREATE PROCEDURE [dbo].[SP_X]\nAS\nSELECT 1;"
    right = "CREATE PROCEDURE [dbo].[SP_X]\nAS\nSELECT 1;"
    _, st = fmt.build_diff(left, right)
    assert st.left_preamble == 1, "comment lines above the header count as residue"
    assert st.is_identical


def test_text_without_a_module_header_is_left_alone():
    left = "SELECT 1;\nSELECT 2;"
    right = "SELECT 1;\nSELECT 3;"
    rows, st = fmt.build_diff(left, right)
    assert st.left_preamble == 0 and st.right_preamble == 0
    assert st.changed == 1
    assert [r[1] for r in rows] == [1, 2]


def test_canonical_lines_merges_only_the_header():
    lines, preamble = fmt.canonical_lines(PROD_SHAPE)
    assert preamble == 1
    assert lines[0][1].startswith("CREATE PROCEDURE [dbo].[SP_X]")
    # the body lines keep their own identity and original numbering
    assert [n for n, _, _ in lines[1:]] == [7, 8, 9, 10, 11, 12]


# --------------------------------------------------------------------------- #
# Every statement is covered (v1.2) - DDL, data scripts, backups and checks are
# compared batch by batch, and duplicates are reported, not collapsed.
# --------------------------------------------------------------------------- #

import compare  # noqa: E402
from sources import classify_statement, find_statements, load_side  # noqa: E402

RELEASE = """\
-- banner
GO
SELECT * INTO UWDataStaging..TEMP_X_BACKUP FROM dbo.X
GO
IF COL_LENGTH('dbo.X', 'C') IS NULL
  ALTER TABLE dbo.X ADD C INT NULL;
GO
CREATE OR ALTER PROCEDURE dbo.SP_A AS SELECT 1;
GO
-- a comment
DELETE r FROM dbo.X r WHERE r.C = 1;
GO
"""


def test_statements_are_found_and_modules_excluded():
    stmts, comment_only = find_statements(RELEASE, "r.sql")
    assert [s.kind for s in stmts] == ["SELECT INTO", "ALTER TABLE", "DELETE"]
    assert comment_only == 1
    assert stmts[0].line == 3


def test_delete_alias_resolves_to_the_table():
    kind, targets, _ = classify_statement("DELETE r FROM dbo.X r WHERE r.C = 1;")
    assert (kind, targets) == ("DELETE", ["dbo.X"])


def test_temp_tables_are_not_targets():
    kind, targets, _ = classify_statement(
        "SELECT * INTO #t FROM dbo.Y; DROP TABLE IF EXISTS #t;")
    assert kind == "SELECT" and targets == ["dbo.Y"]


@pytest.mark.parametrize("sql,kind", [
    ("CREATE TABLE dbo.T (Id INT)", "CREATE TABLE"),
    ("CREATE NONCLUSTERED INDEX IX ON dbo.T (Id)", "CREATE INDEX"),
    ("ALTER FULLTEXT INDEX ON dbo.T SET STOPLIST = OFF", "FULLTEXT INDEX"),
    ("INSERT INTO dbo.T VALUES (1)", "INSERT"),
    ("UPDATE dbo.T SET Id = 2", "UPDATE"),
    ("MERGE dbo.T AS t USING s ON 1=0 WHEN NOT MATCHED THEN INSERT VALUES (1);", "MERGE"),
    ("DROP PROCEDURE IF EXISTS dbo.SP_OLD", "DROP MODULE"),
    ("EXEC dbo.SP_X 1", "EXEC"),
    ("PRINT 'hi'", "PRINT"),
])
def test_statement_kinds(sql, kind):
    assert classify_statement(sql)[0] == kind


def _stmts(text):
    return find_statements(text, "x.sql")[0]


def test_pairing_identical_changed_new_missing():
    left = _stmts("UPDATE dbo.T SET A = 1 WHERE Id = 5;\nGO\n"
                  "INSERT INTO dbo.Gone VALUES (1);\nGO\n")
    right = _stmts("UPDATE dbo.T SET A = 2 WHERE Id = 5;\nGO\nALTER TABLE dbo.New ADD C INT;\nGO\n")
    res = {r["verdict"]: r for r in compare.pair_statements(left, right)}
    assert set(res) == {"changed", "new", "missing"}
    assert res["changed"]["stats"].changed == 1


def test_comment_only_change_is_reported_not_hidden():
    left = _stmts("-- old note\nUPDATE dbo.T SET A = 1;\nGO\n")
    right = _stmts("-- new note\nUPDATE dbo.T SET A = 1;\nGO\n")
    (r,) = compare.pair_statements(left, right)
    assert r["verdict"] == "changed" and r["comments_only"]


def test_statement_diff_uses_real_file_line_numbers():
    left = _stmts("\n\n\nUPDATE dbo.T SET A = 1;\nGO\n")
    right = _stmts("UPDATE dbo.T SET A = 2;\nGO\n")
    (r,) = compare.pair_statements(left, right)
    assert r["rows"][0][1] == 4 and r["rows"][0][3] == 1


def test_duplicate_module_and_statement_are_detected(tmp_path: Path):
    f = tmp_path / "dup.sql"
    f.write_text("CREATE PROCEDURE dbo.SP_A AS SELECT 1;\nGO\nUPDATE dbo.T SET A = 1;\nGO\n"
                 "ALTER PROCEDURE dbo.SP_A AS SELECT 2;\nGO\n"
                 "UPDATE dbo.T  SET A = 1; -- again\nGO\n",
                 encoding="utf-8")
    side = load_side(f"file:{f}")
    assert side.duplicate_modules() == {"SP_A": [1, 5]}
    assert len(side.duplicate_statements()) == 1
    assert len(compare.duplicate_findings(side, "file")) == 2


def test_dump_side_has_no_statements(tmp_path: Path):
    d = tmp_path / "d.json"
    d.write_text(json.dumps({"SP_A": "CREATE PROCEDURE dbo.SP_A AS SELECT 1;"}), encoding="utf-8")
    assert load_side(f"dump:{d}").statements is None


def test_cli_end_to_end_reports_statements(tmp_path: Path):
    left, right = tmp_path / "l.sql", tmp_path / "r.sql"
    left.write_text("ALTER TABLE dbo.X ADD C INT;\nGO\n", encoding="utf-8")
    right.write_text(RELEASE, encoding="utf-8")
    out, js = tmp_path / "r.html", tmp_path / "s.json"
    import subprocess
    subprocess.run([sys.executable, str(SCRIPTS / "compare.py"), "--left", f"file:{left}",
                    "--right", f"file:{right}", "--out", str(out), "--json", str(js)],
                   check=True, capture_output=True)
    s = json.loads(js.read_text("utf-8"))
    assert s["format_version"] == "1.2"
    assert s["statement_summary"] == {"changed": 1, "identical": 0, "new": 2,
                                      "missing": 0, "verify": 0}
    html = out.read_text("utf-8")
    assert "Every statement at a glance" in html and "Duplicates" in html
