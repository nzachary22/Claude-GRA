"""Privacy gate: queries that must be refused, and reporting queries that must work."""

import sqlite3

import pytest

from gra.gate import QueryGate, QueryRefused


@pytest.fixture
def gate(tiny_db):
    db, catalog = tiny_db
    return QueryGate(db, catalog)


# ------------------------------------------------------------------ allowed

ALLOWED = {
    "simple count": "SELECT COUNT(*) AS n FROM cases",
    "count of one is fine": "SELECT COUNT(*) FROM clients WHERE gender = 'nonbinary'",
    "zero count is fine": "SELECT COUNT(*) FROM clients WHERE gender = 'nobody'",
    "distinct clients": "SELECT COUNT(DISTINCT client_id) FROM cases",
    "group by category": "SELECT gender, COUNT(*) AS n FROM clients GROUP BY gender ORDER BY gender",
    "join on ids": "SELECT c.gender, COUNT(DISTINCT k.case_id) FROM cases k JOIN clients c ON c.client_id = k.client_id GROUP BY c.gender",
    "date bucket": "SELECT strftime('%m', opened) AS month, COUNT(*) FROM cases GROUP BY month",
    "age bands from birth date": (
        "SELECT CASE WHEN date_of_birth > '2000-01-01' THEN 'under 25' ELSE '25+' END AS band, COUNT(*) "
        "FROM clients GROUP BY band"),
    "age in years is bucketed": (
        "SELECT CAST((julianday('2025-12-31') - julianday(date_of_birth)) / 365.25 AS INTEGER) AS age, COUNT(*) "
        "FROM clients GROUP BY age"),
    "cte with per-client step": (
        "WITH per AS (SELECT client_id, COUNT(*) AS n FROM cases GROUP BY client_id) "
        "SELECT n, COUNT(*) AS clients FROM per GROUP BY n"),
    "latest record per client via window": (
        "WITH r AS (SELECT client_id, gender, ROW_NUMBER() OVER (PARTITION BY client_id ORDER BY client_id) AS rn "
        "FROM clients) SELECT gender, COUNT(*) FROM r WHERE rn = 1 GROUP BY gender"),
    "in subquery": "SELECT COUNT(*) FROM cases WHERE client_id IN (SELECT client_id FROM clients WHERE county = 'North')",
    "exists correlated": (
        "SELECT COUNT(*) FROM clients c WHERE EXISTS (SELECT 1 FROM cases k WHERE k.client_id = c.client_id AND k.closed IS NULL)"),
    "union total row": (
        "SELECT problem_code, COUNT(*) FROM cases GROUP BY problem_code UNION ALL SELECT 'Total', COUNT(*) FROM cases"),
    "sum of minutes": "SELECT problem_code, SUM(minutes), AVG(minutes) FROM cases GROUP BY problem_code",
    "reference table read": "SELECT problem_code, label, COUNT(*) FROM problem_codes GROUP BY problem_code, label",
    "outer select over aggregated subqueries": (
        "SELECT a.gender, a.n FROM (SELECT gender, COUNT(*) AS n FROM clients GROUP BY gender) a"),
    "blank checks": "SELECT COUNT(*) FROM cases WHERE closed IS NULL AND client_id IS NOT NULL",
    "max of a case date": "SELECT MAX(opened) FROM cases",
}


@pytest.mark.parametrize("sql", ALLOWED.values(), ids=ALLOWED.keys())
def test_allowed(gate, sql):
    result = gate.run(sql)
    assert result.rows is not None


def test_counts_match_direct_sql(gate, tiny_db):
    db, _ = tiny_db
    result = gate.run("SELECT gender, COUNT(*) AS n FROM clients GROUP BY gender ORDER BY gender")
    direct = sqlite3.connect(db).execute(
        "SELECT gender, COUNT(*) FROM clients GROUP BY gender ORDER BY gender").fetchall()
    assert [tuple(r) for r in result.rows] == direct


def test_log_records_queries(gate):
    r = gate.run("SELECT COUNT(*) FROM cases", purpose="total")
    assert r.query_id == "Q1" and gate.log[0].purpose == "total"


# ------------------------------------------------------------------ refused

REFUSED = {
    "select star": "SELECT * FROM clients",
    "raw rows": "SELECT gender, county FROM clients",
    "select id": "SELECT client_id FROM clients",
    "group by id": "SELECT client_id, COUNT(*) FROM cases GROUP BY client_id",
    "group by name": "SELECT last_name, COUNT(*) FROM clients GROUP BY last_name",
    "bare column with aggregate": "SELECT client_id, COUNT(*) FROM cases",
    "probe by name": "SELECT COUNT(*) FROM clients WHERE last_name = 'Stone'",
    "probe by id": "SELECT COUNT(*) FROM cases WHERE client_id = 'C-001'",
    "probe with like": "SELECT COUNT(*) FROM clients WHERE first_name LIKE 'A%'",
    "probe with in list": "SELECT COUNT(*) FROM clients WHERE client_id IN ('C-001', 'C-002')",
    "substring of id": "SELECT substr(client_id, 1, 3) AS p, COUNT(*) FROM clients GROUP BY p",
    "min of name": "SELECT MIN(last_name) FROM clients",
    "max of birth date": "SELECT MAX(date_of_birth) FROM clients",
    "raw birth date groups": "SELECT date_of_birth, COUNT(*) FROM clients GROUP BY date_of_birth",
    "group_concat": "SELECT GROUP_CONCAT(gender) FROM clients",
    "group_concat of id": "SELECT GROUP_CONCAT(client_id) FROM clients",
    "notes column": "SELECT COUNT(*) FROM clients WHERE intake_notes LIKE '%eviction%'",
    "notes output": "SELECT intake_notes, COUNT(*) FROM clients GROUP BY intake_notes",
    "id through cte alias": "WITH x AS (SELECT client_id AS k FROM clients) SELECT k, COUNT(*) FROM x GROUP BY k",
    "probe through cte alias": "WITH x AS (SELECT client_id AS k FROM clients) SELECT COUNT(*) FROM x WHERE k = 'C-001'",
    "per-client listing via subquery": "SELECT n FROM (SELECT client_id, COUNT(*) AS n FROM cases GROUP BY client_id)",
    "scalar subquery leaks id": "SELECT (SELECT client_id FROM clients LIMIT 1) AS x, COUNT(*) FROM cases",
    "disguised listing by raw minutes": "SELECT minutes, opened, COUNT(*) FROM cases GROUP BY minutes, opened",
    "pragma": "PRAGMA table_info(clients)",
    "attach": "ATTACH DATABASE 'x.db' AS x",
    "insert": "INSERT INTO cases (case_id) VALUES ('K-9')",
    "delete": "DELETE FROM cases",
    "drop": "DROP TABLE cases",
    "two statements": "SELECT COUNT(*) FROM cases; SELECT COUNT(*) FROM clients",
    "sqlite_master": "SELECT COUNT(*) FROM sqlite_master",
    "internal catalog": "SELECT COUNT(*) FROM _gra_catalog",
    "load_extension": "SELECT load_extension('evil')",
    "hex of id": "SELECT hex(client_id) AS h, COUNT(*) FROM clients GROUP BY h",
    "case returning id": "SELECT CASE WHEN 1=1 THEN client_id END AS x, COUNT(*) FROM clients GROUP BY x",
    "id compared to expression": "SELECT COUNT(*) FROM clients WHERE client_id = 'C-' || '001'",
    "unknown column": "SELECT COUNT(nope) FROM clients",
}


@pytest.mark.parametrize("sql", REFUSED.values(), ids=REFUSED.keys())
def test_refused(gate, sql):
    with pytest.raises(QueryRefused):
        gate.run(sql)


def test_refusal_messages_never_contain_data_values(gate):
    for sql in REFUSED.values():
        try:
            gate.run(sql)
        except QueryRefused as e:
            for secret in ("Stone", "Ava", "1980-02-01", "mold"):
                if secret not in sql:
                    assert secret not in str(e)


def test_row_cap(tiny_db):
    db, catalog = tiny_db
    gate = QueryGate(db, catalog, max_rows=2)
    with pytest.raises(QueryRefused):
        gate.run("SELECT problem_code, COUNT(*) FROM cases GROUP BY problem_code")


def test_database_is_never_modified(gate, tiny_db):
    db, _ = tiny_db
    before = db.read_bytes()
    for sql in REFUSED.values():
        try:
            gate.run(sql)
        except QueryRefused:
            pass
    assert db.read_bytes() == before


def test_out_of_memory_is_refused_not_a_crash(gate, monkeypatch):
    real = gate._connect

    class Conn:
        def __init__(self):
            self.c = real()

        def execute(self, sql):
            raise MemoryError()

        def set_progress_handler(self, *a):
            pass

        def close(self):
            self.c.close()

    monkeypatch.setattr(gate, "_connect", Conn)
    with pytest.raises(QueryRefused, match="more memory"):
        gate.run("SELECT COUNT(*) AS n FROM cases")
