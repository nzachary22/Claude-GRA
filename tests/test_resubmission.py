"""Submitting tables: good tables are kept, only failing ones are resent, and a table
that keeps failing is simplified and finally shown as not completed."""

import copy

from gra.engine import MAX_TABLE_ATTEMPTS
from gra.tables import resolve_tables
from tests.test_engine import GENDER_SQL, gender_table, make

QUERY = [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})]


def table(tid, title=None):
    t = gender_table()
    t["id"], t["title"] = tid, title or f"Table {tid}"
    return t


def broken(tid):
    t = table(tid)
    t["rows"][0]["cells"] = []  # wrong number of cells
    return t


def ids(session):
    return [t.id for t in session.tables]


def test_good_tables_are_kept_and_only_the_failing_one_is_resent(tiny_db):
    seen = []

    def resend(results):
        seen.append(results[0])
        return [("submit_tables", {"tables": [table("b")]})]

    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [table("a"), broken("b"), table("c")]})],
        resend,
    ])
    session.run()
    assert session.status == "done"
    assert ids(session) == ["a", "b", "c"]  # report order kept, nothing dropped
    feedback = seen[0]
    assert "Saved 2 table(s)" in feedback and "a, c" in feedback
    assert "Resend ONLY" in feedback and "Table b" in feedback and "attempt 1 of 5" in feedback
    assert "has 0 cells but the table has 1 columns" in feedback


def test_resending_only_the_fixed_table_never_drops_the_others(tiny_db):
    """The clinic run: the AI resent only the fixed table and the app kept just that one."""
    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [broken("csr"), table("profile"), table("county")]})],
        [("submit_tables", {"tables": [table("csr")]})],
    ])
    session.run()
    assert ids(session) == ["csr", "profile", "county"]


def test_advice_escalates_and_the_table_is_shown_as_not_completed_after_five_tries(tiny_db):
    feedback = []

    def again(results):
        feedback.append(results[0])
        return [("submit_tables", {"tables": [broken("b")]})]

    session, _ = make(tiny_db, [QUERY, [("submit_tables", {"tables": [table("a"), broken("b")]})]]
                      + [again] * (MAX_TABLE_ATTEMPTS - 1))
    session.run()
    assert session.status == "done"
    assert len(feedback) == MAX_TABLE_ATTEMPTS - 1
    assert "Advice" not in feedback[0]  # attempt 2: just fix it
    assert "Advice for attempt 3" in feedback[1] and '"gap"' in feedback[1]
    assert "Advice for attempt 4" in feedback[2] and "Partially completed:" in feedback[2]
    assert "Advice for attempt 5, the last" in feedback[3] and "not completed" in feedback[3]
    assert ids(session) == ["a", "b"]
    stand_in = session.tables[1]
    assert stand_in.title == "Table b"
    assert stand_in.rows[0].label == "Not completed" and stand_in.rows[0].cells[0].gap
    assert "could not be completed" in stand_in.note
    note = session.notes["app-incomplete-b"]
    assert note.kind == "warning" and note.tables == ["b"]


def test_a_table_passing_after_simplifying_is_flagged(tiny_db):
    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [table("b")]})],  # attempt 4, stripped down
    ])
    session.run()
    assert session.status == "done"
    assert session.notes["app-simplified-b"].kind == "warning"
    assert session.tables[0].warnings


def test_a_table_passing_on_attempt_two_or_three_is_not_flagged(tiny_db):
    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [table("b")]})],
    ])
    session.run()
    assert session.status == "done" and not session.notes


def test_simplified_flag_defers_to_the_ais_own_warning(tiny_db):
    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [broken("b")]})],
        [("submit_tables", {"tables": [broken("b")]})],
        [("note_assumption", {"id": "W1", "kind": "warning", "text": "Partially completed: rows merged.",
                              "tables": ["b"]}),
         ("submit_tables", {"tables": [table("b")]})],
    ])
    session.run()
    assert "app-simplified-b" not in session.notes and "W1" in session.notes


def test_correction_keeps_unchanged_tables_and_can_remove_one(tiny_db):
    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [table("a"), table("b"), table("c")]})],
        [("submit_tables", {"tables": [table("b", "Renamed b")], "remove_tables": ["c"]})],
    ])
    session.run()
    session.correct("Rename table b and drop table c.")
    assert session.status == "done"
    assert ids(session) == ["a", "b"]
    assert session.tables[1].title == "Renamed b"


def test_a_correction_that_keeps_failing_leaves_the_earlier_table(tiny_db):
    session, _ = make(tiny_db, [QUERY, [("submit_tables", {"tables": [table("a")]})]]
                      + [[("submit_tables", {"tables": [broken("a")]})]] * MAX_TABLE_ATTEMPTS)
    session.run()
    session.correct("Change table a.")
    assert session.status == "done"
    assert session.tables[0].rows[0].label == "Women"  # the earlier version, not a stand-in
    assert "before" in session.notes["app-incomplete-a"].text


def test_new_table_after_a_retry_keeps_report_order(tiny_db):
    session, _ = make(tiny_db, [
        QUERY,
        [("submit_tables", {"tables": [table("a"), broken("b"), table("c")]})],
        [("submit_tables", {"tables": [table("b")]})],
    ])
    session.run()
    assert ids(session) == ["a", "b", "c"]


# ------------------------------------------------------------ whole-row gaps

def test_whole_row_gap_fills_every_cell(tiny_db):
    session, _ = make(tiny_db, [QUERY])
    session.run()  # stops without submitting; the query result is what we need
    results = {r.query_id: r for r in session.gate.log}
    spec = {"id": "csr", "title": "CSR", "columns": [{"header": "Advice"}, {"header": "Limited"},
                                                       {"header": "Total", "sum_of_columns": [0, 1]}],
            "rows": [{"label": "Bankruptcy", "kind": "detail", "gap": "Problem not coded"},
                     {"label": "Women", "kind": "detail", "cells": [
                         {"query": "Q1", "match": {"gender": "woman"}, "column": "n"}, {"text": "—"}, None]},
                     {"label": "Total", "kind": "total", "sum_of_rows": [0, 1]}]}
    (t,) = resolve_tables([spec], results)
    row = t.rows[0]
    assert [c.gap for c in row.cells[:2]] == ["Problem not coded"] * 2
    assert row.cells[2].gap  # the total of unavailable figures is unavailable too
    assert all(c.display == "n/a" for c in row.cells)
    assert t.rows[2].cells[0].gap  # column total includes an unavailable figure


def test_whole_row_gap_needs_a_reason_and_no_cells(tiny_db):
    session, _ = make(tiny_db, [QUERY])
    session.run()
    results = {r.query_id: r for r in session.gate.log}
    base = {"id": "x", "title": "X", "columns": [{"header": "A"}], "rows": []}
    for row, expected in (({"label": "R", "kind": "detail", "gap": ""}, "reason as text"),
                          ({"label": "R", "kind": "detail", "gap": "why", "cells": [{"text": "a"}]}, "not both")):
        spec = copy.deepcopy(base)
        spec["rows"] = [row]
        try:
            resolve_tables([spec], results)
            raise AssertionError("expected a problem")
        except Exception as e:  # TableSpecError
            assert expected in str(e)
