import pytest

from gra.gate import QueryResult
from gra.tables import TableSpecError, resolve_tables


@pytest.fixture
def results():
    return {
        "Q1": QueryResult("Q1", "", "", ["gender", "age", "n"],
                          [["woman", "adult", 5], ["woman", "child", 1], ["man", "adult", 3]]),
        "Q2": QueryResult("Q2", "", "", ["avg_minutes"], [[42.5]]),
    }


def cell(**match):
    return {"query": "Q1", "match": match, "column": "n", "missing_means_zero": True}


def spec():
    return [{
        "id": "t1", "title": "Clients by gender and age",
        "columns": [{"header": "Adult"}, {"header": "Child"}, {"header": "Total", "sum_of_columns": [0, 1]}],
        "rows": [
            {"label": "Women", "kind": "detail", "cells": [cell(gender="woman", age="adult"), cell(gender="woman", age="child"), None]},
            {"label": "Men", "kind": "detail", "cells": [cell(gender="man", age="adult"), cell(gender="man", age="child"), None]},
            {"label": "Total", "kind": "total", "sum_of_rows": [0, 1]},
        ],
    }]


def values(table):
    return [[c.value for c in r.cells] for r in table.rows]


def test_values_come_from_results_and_sums_are_computed(results):
    (t,) = resolve_tables(spec(), results)
    assert values(t) == [[5, 1, 6], [3, 0, 3], [8, 1, 9]]
    assert t.rows[1].cells[1].source == "Q1"


def test_literal_numbers_are_rejected(results):
    s = spec()
    s[0]["rows"][0]["cells"][0] = 99
    with pytest.raises(TableSpecError, match="literal"):
        resolve_tables(s, results)
    s[0]["rows"][0]["cells"][0] = {"text": "99"}
    with pytest.raises(TableSpecError, match="numbers cannot"):
        resolve_tables(s, results)


def test_missing_group_is_not_zero_unless_declared(results):
    s = spec()
    s[0]["rows"][1]["cells"][1] = {"query": "Q1", "match": {"gender": "man", "age": "child"}, "column": "n"}
    with pytest.raises(TableSpecError, match="missing_means_zero"):
        resolve_tables(s, results)


def test_gaps_propagate_to_totals(results):
    s = spec()
    s[0]["rows"][1]["cells"][1] = {"gap": "Age not recorded"}
    (t,) = resolve_tables(s, results)
    assert t.rows[1].cells[1].gap == "Age not recorded"
    assert t.rows[1].cells[2].gap and t.rows[2].cells[1].gap
    assert t.rows[2].cells[0].value == 8


def test_ambiguous_match_and_unknown_query(results):
    s = spec()
    s[0]["rows"][0]["cells"][0] = {"query": "Q1", "match": {"gender": "woman"}, "column": "n"}
    s[0]["rows"][1]["cells"][0] = {"query": "Q9", "column": "n"}
    with pytest.raises(TableSpecError) as e:
        resolve_tables(s, results)
    assert any("hits 2 rows" in p for p in e.value.problems)
    assert any("unknown query" in p for p in e.value.problems)


def test_totals_skip_not_applicable_text(results):
    def dash():
        return {"text": "—"}
    s = [{"id": "t", "title": "Wages", "columns": [{"header": "Wages", "format": "currency", "decimals": 2}],
          "rows": [{"label": "Paid", "kind": "detail", "cells": [cell(gender="woman", age="adult")]},
                   {"label": "Group A", "kind": "subtotal", "sum_of_rows": [0]},
                   {"label": "Other", "kind": "detail", "cells": [dash()]},
                   {"label": "Group B", "kind": "subtotal", "sum_of_rows": [2]},
                   {"label": "All", "kind": "total", "sum_of_rows": [1, 3]}]}]
    (t,) = resolve_tables(s, results)
    shown = [r.cells[0].display for r in t.rows]
    assert shown == ["$5.00", "$5.00", "—", "—", "$5.00"]
    assert t.rows[4].cells[0].value == 5 and not t.rows[4].cells[0].gap


def test_gaps_still_block_totals_even_beside_text(results):
    s = [{"id": "t", "title": "Wages", "columns": [{"header": "Wages"}],
          "rows": [{"label": "A", "kind": "detail", "cells": [{"gap": "Unknown"}]},
                   {"label": "B", "kind": "detail", "cells": [{"text": "—"}]},
                   {"label": "All", "kind": "total", "sum_of_rows": [0, 1]}]}]
    (t,) = resolve_tables(s, results)
    assert t.rows[2].cells[0].gap


def test_formats_cell_beats_row_beats_column(results):
    s = spec()
    s[0]["columns"][0].update(format="percent", decimals=1)
    s[0]["rows"][1]["format"] = "currency"
    s[0]["rows"][1]["decimals"] = 0
    s[0]["rows"][1]["cells"][1]["format"] = "number"
    (t,) = resolve_tables(s, results)
    assert t.rows[0].cells[0].display == "5.0%"
    assert t.rows[1].cells[0].display == "$3"
    assert t.rows[1].cells[1].display == "0"
    assert t.rows[2].cells[0].display == "8.0%"  # computed totals take the column format
    assert t.rows[0].cells[1].display == "1"


def test_unknown_format_is_rejected(results):
    s = spec()
    s[0]["columns"][0]["format"] = "fancy"
    with pytest.raises(TableSpecError, match="unknown format"):
        resolve_tables(s, results)


def test_row_header_and_bound_text(results):
    s = spec()
    s[0]["row_header"] = "Gender"
    s[0]["subtitle"] = "Women who are adults: {n}"
    s[0]["note"] = "Average {avg} minutes."
    s[0]["values"] = {"n": cell(gender="woman", age="adult"),
                      "avg": {"query": "Q2", "column": "avg_minutes", "decimals": 1}}
    (t,) = resolve_tables(s, results)
    assert t.row_header == "Gender"
    assert t.subtitle == "Women who are adults: 5"
    assert t.note == "Average 42.5 minutes."


def test_text_placeholders_must_be_bound_to_results(results):
    s = spec()
    s[0]["subtitle"] = "Clients: {clients}"
    with pytest.raises(TableSpecError, match="no binding"):
        resolve_tables(s, results)
    s[0]["values"] = {"clients": {"query": "Q9", "column": "n"}}
    with pytest.raises(TableSpecError, match="unknown query"):
        resolve_tables(s, results)


def test_single_value_query_and_decimals(results):
    s = [{"id": "t2", "title": "Average", "columns": [{"header": "Minutes"}],
          "rows": [{"label": "Average", "cells": [{"query": "Q2", "decimals": 0}]}]}]
    (t,) = resolve_tables(s, results)
    assert t.rows[0].cells[0].value == 42.0 or t.rows[0].cells[0].value == 42
