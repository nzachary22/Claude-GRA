import json
import sqlite3

from gra.engine import Limits, ReportSession
from gra.gate import QueryGate
from gra.schema import describe
from tests.scripted import ScriptedModel

GENDER_SQL = "SELECT gender, COUNT(*) AS n FROM clients GROUP BY gender"


def gender_table(qid="Q1"):
    def c(g):
        return {"query": qid, "match": {"gender": g}, "column": "n", "missing_means_zero": True}
    return {"id": "t1", "title": "Clients by gender", "columns": [{"header": "Clients"}],
            "rows": [{"label": "Women", "kind": "detail", "cells": [c("woman")]},
                     {"label": "Men", "kind": "detail", "cells": [c("man")]},
                     {"label": "Nonbinary", "kind": "detail", "cells": [c("nonbinary")]},
                     {"label": "Not recorded", "kind": "detail", "cells": [c(None)]},
                     {"label": "Total", "kind": "total", "sum_of_rows": [0, 1, 2, 3]}]}


def make(tiny_db, steps, limits=None):
    db, catalog = tiny_db
    gate = QueryGate(db, catalog)
    model = ScriptedModel(steps)
    session = ReportSession(gate, describe(db, catalog), [{"type": "text", "text": "Report: clients by gender."}],
                            ("2025-01-01", "2025-12-31"), model, limits)
    return session, model


def test_full_loop_builds_tables_from_local_results(tiny_db):
    session, model = make(tiny_db, [
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [("note_assumption", {"id": "A1", "kind": "assumption", "text": "Blank gender shown as Not recorded.",
                              "tables": ["t1"], "how_to_change": "Say how blanks should be reported."}),
         ("submit_tables", {"tables": [gender_table()]})],
    ])
    session.run()
    assert session.status == "done"
    db, _ = tiny_db
    direct = dict(sqlite3.connect(db).execute(GENDER_SQL).fetchall())
    vals = [r.cells[0].value for r in session.tables[0].rows]
    assert vals == [direct["woman"], direct["man"], direct["nonbinary"], direct[None], sum(direct.values())]
    assert session.notes["A1"].kind == "assumption"
    assert any(e.kind == "query" and "gender" in e.message for e in session.events)


def test_ai_never_receives_identifier_values_or_notes(tiny_db):
    session, model = make(tiny_db, [
        [("run_query", {"sql": "SELECT client_id FROM clients", "purpose": "probe"}),
         ("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})],
        [("submit_tables", {"tables": [gender_table("Q1")]})],
    ])
    session.run()
    sent = json.dumps([m for call in model.seen_messages for m in call], default=str)
    for secret in ("C-001", "Stone", "Ava", "1980-02-01", "mold", "Synthetic intake narrative"):
        assert secret not in sent
    assert session.status == "done"


def test_rejected_submission_is_explained_and_retried(tiny_db):
    bad = gender_table()
    bad["rows"][0]["cells"] = [42]

    def check_feedback(results):
        assert "literal" in results[0]
        return [("submit_tables", {"tables": [gender_table()]})]

    session, _ = make(tiny_db, [
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
        [("submit_tables", {"tables": [bad]})],
        check_feedback,
    ])
    session.run()
    assert session.status == "done"


def test_query_budget_and_turn_limits(tiny_db):
    loop = [[("run_query", {"sql": GENDER_SQL, "purpose": "again"})]] * 10
    session, _ = make(tiny_db, loop, Limits(max_queries=2, max_turns=4))
    session.run()
    assert session.status == "failed" and session.queries_used == 2


def test_cancel_stops_the_run(tiny_db):
    session, _ = make(tiny_db, [[("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})]] * 5)
    session.cancel()
    session.run()
    assert session.status == "cancelled"


def test_correction_recalculates(tiny_db):
    session, model = make(tiny_db, [
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
        [("submit_tables", {"tables": [gender_table()]})],
        # after the correction
        [("run_query", {"sql": "SELECT gender, COUNT(*) AS n FROM clients WHERE county = 'North' GROUP BY gender",
                        "purpose": "Counting North clients by gender"})],
        [("submit_tables", {"tables": [gender_table("Q2")]})],
    ])
    session.run()
    first_total = session.tables[0].rows[-1].cells[0].value
    session.correct("Only include clients from North county.")
    assert session.status == "done"
    assert session.tables[0].rows[-1].cells[0].value < first_total
    last_user = model.seen_messages[2][-1]
    assert "North county" in json.dumps(last_user)


def test_report_name_is_kept_and_survives_a_correction(tiny_db):
    session, _ = make(tiny_db, [
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
        [("submit_tables", {"report": {"organization": "Example Center", "title": "Annual report", "ignored": "x"},
                            "tables": [gender_table()]})],
        [("submit_tables", {"tables": [gender_table()]})],
    ])
    session.run()
    assert session.report == {"organization": "Example Center", "title": "Annual report"}
    session.correct("Keep everything the same.")
    assert session.snapshot()["report"]["organization"] == "Example Center"


def test_schema_description_hides_values(tiny_db):
    db, catalog = tiny_db
    text = describe(db, catalog)
    assert "gender" in text and "'woman'" in text
    assert "intake_notes" not in text and "free-text column" in text
    for secret in ("C-001", "Stone", "1980-02-01"):
        assert secret not in text
    assert "Housing" in text  # reference table contents are shared


def test_report_definitions_are_kept(tiny_db):
    session, _ = make(tiny_db, [
        [("run_query", {"sql": GENDER_SQL, "purpose": "Counting"})],
        [("submit_tables", {"report": {"title": "Annual report", "definitions": [
            {"heading": "What the figures count", "text": "Closed cases in the period."}, {"heading": "empty", "text": " "},
            "not a definition"]}, "tables": [gender_table()]})],
    ])
    session.run()
    assert session.report["definitions"] == [{"heading": "What the figures count", "text": "Closed cases in the period."}]
