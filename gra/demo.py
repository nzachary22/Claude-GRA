"""The no-key demo: a recorded analyst for the fictional Juniper Ridge sample.

It plays the AI's part for the bundled sample (synthetic data, invented
organization), so anyone can watch the whole process and get finished tables
without an API key, cost, or anything leaving the computer. The SQL was written
from the sample report's printed definitions (page 8 of the 2025 PDF) and is
generic over the reporting year. It runs through the same privacy gate and table
assembly as a live model. The tests use it too, to check every figure.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from types import SimpleNamespace

RACE_ROWS = [
    ("hispanic", "Hispanic / Latino, any race"),
    ("black", "Black / African American, non-Hispanic"),
    ("white", "White, non-Hispanic"),
    ("asian", "Asian, non-Hispanic"),
    ("nhpi", "Native Hawaiian / Pacific Islander, non-Hispanic"),
    ("aian", "American Indian / Alaska Native, non-Hispanic"),
    ("multiple", "Multiple races, non-Hispanic"),
    ("self_described", "Another self-described race, non-Hispanic"),
    ("not_reported", "Race / ethnicity not reported"),
    ("unknown", "Race / ethnicity unknown or unusable"),
]
AGE_COLS = [("under18", "Under 18"), ("18_59", "18–59"), ("60plus", "60 and Over"), ("unknown", "Age unknown")]
GENDER_COLS = [("woman", "Women"), ("man", "Men"), ("nonbinary", "Nonbinary"), ("self_described", "Another self-description"),
               ("not_reported", "Not reported"), ("unknown", "Unknown / unusable")]


def population_cte(start: str, end: str) -> str:
    freeze = f"{int(end[:4]) + 1}-01-31"
    return f"""
WITH ev AS (
  SELECT case_id, event_type, effective_date,
         ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY effective_date DESC, recorded_date DESC) AS rn
  FROM case_events WHERE recorded_date <= '{freeze}'),
last_ev AS (SELECT case_id, event_type, effective_date FROM ev WHERE rn = 1),
q AS (
  SELECT c.case_id, c.primary_client_id, c.primary_problem_code, c.final_appellate_decision, c.contested_trial_decision,
         c.uncontested_trial_decision, c.agency_decision, c.negotiated_settlement, c.court_action_filed,
         c.limited_intervention, c.advice_given, c.substantive_minutes
  FROM cases c
  JOIN last_ev l ON l.case_id = c.case_id
  JOIN clients k ON k.client_id = c.primary_client_id
  WHERE l.event_type = 'close' AND l.effective_date BETWEEN '{start}' AND '{end}'
    AND c.accepted_for_representation = 1 AND c.grant_at_closure = 'housing_stability'
    AND c.civil_case = 1 AND c.duplicate_or_superseded = 0 AND k.client_kind = 'individual'),
dr AS (
  SELECT client_id, birth_date, birth_status, ethnicity, ethnicity_status, race_selections, race_status, gender, gender_status,
         ROW_NUMBER() OVER (PARTITION BY client_id ORDER BY effective_date DESC, recorded_date DESC) AS rn
  FROM demographic_responses WHERE effective_date <= '{end}' AND recorded_date <= '{freeze}'),
pc AS (
  SELECT DISTINCT q.primary_client_id AS client_id FROM q),
demo AS (
  SELECT pc.client_id, d.birth_date, d.birth_status, d.ethnicity, d.ethnicity_status, d.race_selections, d.race_status,
         d.gender, d.gender_status
  FROM pc LEFT JOIN dr d ON d.client_id = pc.client_id AND d.rn = 1)
"""


def table1_sql(start, end):
    return population_cte(start, end) + f"""
SELECT
  CASE
    WHEN ethnicity_status = 'confirmed' AND ethnicity = 'hispanic' THEN 'hispanic'
    WHEN ethnicity_status IN ('declined', 'unanswered') OR race_status IN ('declined', 'unanswered') THEN 'not_reported'
    WHEN ethnicity_status = 'confirmed' AND race_status = 'confirmed' AND ethnicity = 'non_hispanic'
         AND race_selections LIKE '%|%' THEN 'multiple'
    WHEN ethnicity_status = 'confirmed' AND race_status = 'confirmed' AND ethnicity = 'non_hispanic'
         AND race_selections IN ('black', 'white', 'asian', 'nhpi', 'aian', 'self_described') THEN race_selections
    ELSE 'unknown' END AS race,
  CASE
    WHEN birth_status <> 'confirmed' OR birth_date IS NULL OR birth_date > '{end}' THEN 'unknown'
    WHEN (CAST(strftime('%Y', '{end}') AS INTEGER) - CAST(strftime('%Y', birth_date) AS INTEGER)
          - (strftime('%m-%d', '{end}') < strftime('%m-%d', birth_date))) < 18 THEN 'under18'
    WHEN (CAST(strftime('%Y', '{end}') AS INTEGER) - CAST(strftime('%Y', birth_date) AS INTEGER)
          - (strftime('%m-%d', '{end}') < strftime('%m-%d', birth_date))) < 60 THEN '18_59'
    ELSE '60plus' END AS age,
  COUNT(DISTINCT client_id) AS n
FROM demo GROUP BY race, age"""


def table2_sql(start, end):
    return population_cte(start, end) + """
SELECT
  CASE
    WHEN gender_status = 'confirmed' AND gender IN ('woman', 'man', 'nonbinary', 'self_described') THEN gender
    WHEN gender_status IN ('declined', 'unanswered') THEN 'not_reported'
    ELSE 'unknown' END AS gender_cat,
  COUNT(DISTINCT client_id) AS n
FROM demo GROUP BY gender_cat"""


def table3_sql(start, end):
    return population_cte(start, end) + """
SELECT primary_problem_code AS code,
  CASE
    WHEN final_appellate_decision = 1 THEN 'appeals'
    WHEN contested_trial_decision = 1 THEN 'contested'
    WHEN uncontested_trial_decision = 1 THEN 'uncontested'
    WHEN agency_decision = 1 THEN 'agency'
    WHEN negotiated_settlement = 1 AND court_action_filed = 1 THEN 'settlement_litigation'
    WHEN negotiated_settlement = 1 THEN 'settlement_no_litigation'
    WHEN substantive_minutes > 300 THEN 'extensive'
    WHEN limited_intervention = 1 THEN 'limited'
    WHEN advice_given = 1 THEN 'advice'
    ELSE 'other' END AS outcome,
  COUNT(DISTINCT case_id) AS n
FROM q GROUP BY code, outcome"""


def population_sql(start, end):
    return population_cte(start, end) + "SELECT COUNT(DISTINCT case_id) AS cases, COUNT(DISTINCT primary_client_id) AS clients FROM q"


def read_codes(data_dir: Path):
    def rows(name):
        with open(data_dir / name, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    problems = sorted(rows("problem_codes.csv"), key=lambda r: r["sort_order"])
    outcomes = sorted(rows("outcome_codes.csv"), key=lambda r: r["sort_order"])
    return problems, outcomes


def tables_spec(problems, outcomes):
    def b(q, **match):
        return {"query": q, "match": match, "column": "n", "missing_means_zero": True}

    t1_rows = [{"label": label, "kind": "detail", "cells": [b("Q2", race=r, age=a) for a, _ in AGE_COLS] + [None]}
               for r, label in RACE_ROWS]
    t1_rows.append({"label": "Total", "kind": "total", "sum_of_rows": list(range(len(RACE_ROWS)))})
    t1 = {"id": "t1", "title": "01 Race / ethnicity by age",
          "columns": [{"header": h} for _, h in AGE_COLS] + [{"header": "Total", "sum_of_columns": [0, 1, 2, 3]}],
          "rows": t1_rows}
    t2 = {"id": "t2", "title": "02 Gender",
          "columns": [{"header": h} for _, h in GENDER_COLS] + [{"header": "Total", "sum_of_columns": list(range(6))}],
          "rows": [{"label": "Number of Clients", "kind": "detail", "cells": [b("Q3", gender_cat=g) for g, _ in GENDER_COLS] + [None]}]}

    rows, subtotals, groups = [], [], {}
    for p in problems:
        groups.setdefault((p["group_id"], p["group"]), []).append(p)
    for (gid, gname), items in groups.items():
        rows.append({"label": gname, "kind": "heading"})
        first = len(rows)
        for p in items:
            rows.append({"label": f"{p['problem_code']} {p['category']}", "kind": "detail", "indent": 1,
                         "cells": [b("Q4", code=p["problem_code"], outcome=o["outcome_code"]) for o in outcomes] + [None]})
        rows.append({"label": f"{gname} subtotal", "kind": "subtotal", "sum_of_rows": list(range(first, len(rows)))})
        subtotals.append(len(rows) - 1)
    rows.append({"label": "Total", "kind": "total", "sum_of_rows": subtotals})
    t3 = {"id": "t3", "title": "03 Legal problems × case closure outcomes",
          "columns": [{"header": o["label"]} for o in outcomes] + [{"header": "Total", "sum_of_columns": list(range(len(outcomes)))}],
          "rows": rows}
    return [t1, t2, t3]


def steps(start, end, data_dir: Path):
    problems, outcomes = read_codes(data_dir)
    return [
        [("run_query", {"sql": population_sql(start, end), "purpose": "Finding qualifying closed cases and clients"})],
        [("run_query", {"sql": table1_sql(start, end), "purpose": "Counting clients by race / ethnicity and age"}),
         ("run_query", {"sql": table2_sql(start, end), "purpose": "Counting clients by gender"}),
         ("run_query", {"sql": table3_sql(start, end), "purpose": "Counting closed cases by problem and outcome"})],
        [("note_assumption", {"id": "A1", "kind": "assumption", "tables": ["t1", "t2", "t3"],
                              "text": "Records entered after January 31 of the following year are left out, as the "
                                      "report's freeze date rule says.",
                              "how_to_change": "Tell me a different cut-off date."}),
         ("submit_tables", {"tables": tables_spec(problems, outcomes),
                            "report": {"organization": "Juniper Ridge Legal Aid Society",
                                       "title": "Annual civil legal assistance report",
                                       "subtitle": "Invented Housing Stability Grant · fictional sample"}})],
    ]


class DemoModel:
    """Replays the recorded steps in place of an AI service, with short pauses so progress is visible."""

    provider = "demo"
    model = "recorded demo (no AI service)"
    stored = None

    def __init__(self, start: str, end: str, data_dir: Path, pause: float = 1.5):
        self.steps = steps(start, end, data_dir)
        self.calls = 0
        self.pause = pause

    def respond(self, system, messages, tools, should_cancel):
        for _ in range(int(self.pause * 10)):
            if should_cancel():
                break
            time.sleep(0.1)
        step = self.steps[self.calls] if self.calls < len(self.steps) else []
        self.calls += 1
        if not step:
            return SimpleNamespace(content=[{"type": "text", "text": "Done."}], stop_reason="end_turn",
                                   model=self.model, usage={})
        content = [{"type": "tool_use", "id": f"demo{self.calls}_{i}", "name": n, "input": json.loads(json.dumps(inp))}
                   for i, (n, inp) in enumerate(step)]
        return SimpleNamespace(content=content, stop_reason="tool_use", model=self.model, usage={})
