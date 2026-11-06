"""V.1.5.1: every table in the report appears, totals of unavailable figures are not 0,
and the AI is told to write shared documents in neutral wording."""

from gra.engine import SYSTEM_PROMPT
from gra.tables import resolve_tables
from tests.test_engine import GENDER_SQL, gender_table, make

QUERY = [("run_query", {"sql": GENDER_SQL, "purpose": "Counting clients by gender"})]
LISTED = [{"id": "t1", "title": "Clients by gender"}, {"id": "staff", "title": "Staff salaries"}]


def test_a_listed_table_left_out_is_asked_for_once_then_shown_as_not_completed(tiny_db):
    seen = []

    def after(results):
        seen.append(results[0])
        return [("submit_tables", {"tables": [gender_table()], "report_tables": LISTED})]

    session, _ = make(tiny_db, [QUERY, [("submit_tables", {"tables": [gender_table()], "report_tables": LISTED})],
                                after])
    session.run()
    assert session.status == "done", session.error
    assert "staff (Staff salaries)" in str(seen[0]) and "were not submitted" in str(seen[0])
    t = {x.id: x for x in session.tables}
    assert list(t) == ["t1", "staff"] and t["staff"].rows[0].label == "Not completed"
    assert any(n.id == "app-missing-staff" for n in session.notes.values())


def test_a_listed_table_can_be_removed_on_purpose(tiny_db):
    session, _ = make(tiny_db, [QUERY, [("submit_tables", {"tables": [gender_table()], "report_tables": LISTED,
                                                           "remove_tables": ["staff"]})]])
    session.run()
    assert session.status == "done" and [x.id for x in session.tables] == ["t1"]


def test_the_first_submission_must_list_the_report_tables(tiny_db):
    seen = []

    def after(results):
        seen.append(results[0])
        return [("submit_tables", {"tables": [gender_table()], "report_tables": LISTED[:1]})]

    # The scripted model adds report_tables itself unless the step already gives the key.
    session, _ = make(tiny_db, [QUERY, [("submit_tables", {"tables": [gender_table()], "report_tables": []})], after])
    session.run()
    assert session.status == "done" and "report_tables" in str(seen[0])


def test_a_total_over_unavailable_rows_is_unavailable_not_zero(tiny_db):
    class R:  # a stand-in query result
        columns, rows = ["n"], [[0]]
    spec = {"id": "g", "title": "Governance", "columns": [{"header": "Response"}], "rows": [
        {"label": "Board Member Donations", "cells": [{"gap": "No board records"}]},
        {"label": "Largest Single Contribution", "cells": [{"gap": "No board records"}]},
        {"label": "Total Amount Donated by Board Members", "cells": [{"query": "Q1", "column": "n"}]},
        {"label": "Has a written plan?", "cells": [{"gap": "No records"}]}]}
    t = resolve_tables([spec], {"Q1": R()})[0]
    assert t.rows[2].cells[0].gap and t.rows[2].cells[0].value is None


def test_a_real_zero_total_beside_real_figures_stays_zero(tiny_db):
    class R:
        columns, rows = ["n"], [[0]]
    spec = {"id": "g", "title": "Cases", "columns": [{"header": "Cases"}], "rows": [
        {"label": "Housing", "cells": [{"query": "Q1", "column": "n"}]},
        {"label": "Total", "kind": "total", "cells": [{"query": "Q1", "column": "n"}]}]}
    t = resolve_tables([spec], {"Q1": R()})[0]
    assert t.rows[1].cells[0].value == 0 and not t.rows[1].cells[0].gap


def test_prompt_asks_for_every_table_and_neutral_wording():
    assert '"report_tables"' in SYSTEM_PROMPT and "never replace a table with only a note" in SYSTEM_PROMPT
    assert "at your direction" in SYSTEM_PROMPT and "as confirmed by the organization" in SYSTEM_PROMPT
