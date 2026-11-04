import sqlite3

from gra.importer import import_files, load_catalog


def roles(catalog, table):
    return {c.name: c.role for c in catalog.table(table).columns}


def test_columns_are_typed_and_classified(tiny_db):
    db, catalog = tiny_db
    r = roles(catalog, "clients")
    assert r["client_id"] == "identifier"
    assert r["first_name"] == "identifier"
    assert r["last_name"] == "identifier"
    assert r["date_of_birth"] == "sensitive"
    assert r["intake_notes"] == "free_text"
    assert r["gender"] == "category"
    assert r["county"] == "category"
    c = roles(catalog, "cases")
    assert c["case_id"] == "identifier"
    assert c["opened"] == "date"
    assert c["minutes"] == "measure"
    assert c["problem_code"] == "category"


def test_code_table_is_reference_and_keeps_leading_zeros(tiny_db):
    db, catalog = tiny_db
    codes = catalog.table("problem_codes")
    assert codes.reference
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT problem_code FROM problem_codes ORDER BY 1").fetchall() == [("01",), ("02",), ("03",)]


def test_blanks_become_null_not_zero(tiny_db):
    db, _ = tiny_db
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM clients WHERE gender IS NULL").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM cases WHERE closed IS NULL").fetchone() == (24,)


def test_catalog_round_trips(tiny_db):
    db, catalog = tiny_db
    assert load_catalog(db).to_json() == catalog.to_json()


def test_json_lists_become_pipe_values(tmp_path, json_file):
    catalog = import_files(tmp_path / "j.db", [json_file])
    conn = sqlite3.connect(tmp_path / "j.db")
    assert conn.execute("SELECT langs FROM people ORDER BY person_id").fetchall() == [("en|es",), ("en",)]
    assert roles(catalog, "people")["person_id"] == "identifier"


def test_dollar_text_becomes_numbers(tmp_path):
    from tests.conftest import write_csv
    f = write_csv(tmp_path / "x.csv", ["case_id", "Wages Recovered"],
                  [[f"K{i}", f"${1000 + i:,}.{i % 90 + 10}"] for i in range(200)] + [["K999", ""]])
    catalog = import_files(tmp_path / "x.db", [f])
    col = catalog.table("x").column("wages_recovered")
    assert col.role == "measure" and col.sql_type == "REAL" and "dollar" in col.note
    conn = sqlite3.connect(tmp_path / "x.db")
    assert conn.execute("SELECT wages_recovered FROM x WHERE case_id = 'K0'").fetchone() == (1000.10,)
    assert conn.execute("SELECT wages_recovered FROM x WHERE case_id = 'K999'").fetchone() == (None,)


def test_mixed_dollar_text_stays_as_is(tmp_path):
    from tests.conftest import write_csv
    f = write_csv(tmp_path / "x.csv", ["amount"], [["$10.00"], ["pending"]])
    import_files(tmp_path / "x.db", [f])
    assert sqlite3.connect(tmp_path / "x.db").execute("SELECT amount FROM x ORDER BY 1").fetchall() == [("$10.00",), ("pending",)]


def test_us_and_day_first_dates_become_iso(tmp_path):
    from tests.conftest import write_csv
    f = write_csv(tmp_path / "x.csv", ["us", "day_first", "stamp"],
                  [["03/04/2026", "25/12/2025", "2026-03-04 10:15"], ["12/31/2025", "01/02/2026", "2026-03-05 09:00"]])
    catalog = import_files(tmp_path / "x.db", [f])
    rows = sqlite3.connect(tmp_path / "x.db").execute("SELECT us, day_first FROM x").fetchall()
    assert rows == [("2026-03-04", "2025-12-25"), ("2025-12-31", "2026-02-01")]
    assert catalog.table("x").column("us").role == "date" and "MM/DD/YYYY" in catalog.table("x").column("us").note
    assert "DD/MM/YYYY" in catalog.table("x").column("day_first").note
    assert catalog.table("x").column("stamp").role == "date"


def test_impossible_dates_are_left_alone(tmp_path):
    from tests.conftest import write_csv
    f = write_csv(tmp_path / "x.csv", ["d"], [["02/30/2026"], ["03/01/2026"]])
    import_files(tmp_path / "x.db", [f])
    assert sqlite3.connect(tmp_path / "x.db").execute("SELECT d FROM x ORDER BY 1").fetchall() == [("02/30/2026",), ("03/01/2026",)]


def test_notes_in_a_code_list_are_shared_but_case_notes_are_not(tmp_path):
    from tests.conftest import write_csv
    long = "Multiple selections are separated by a semicolon and reported alone or in combination "
    codes = write_csv(tmp_path / "reference_codes.csv", ["code", "notes"], [[f"C{i}", long + str(i)] for i in range(30)])
    cases = write_csv(tmp_path / "intake_log.csv", ["ref", "notes"], [[f"R{i}", long + str(i)] for i in range(30)])
    catalog = import_files(tmp_path / "x.db", [codes, cases])
    assert catalog.table("reference_codes").reference and catalog.table("reference_codes").column("notes").role == "category"
    assert not catalog.table("intake_log").reference and catalog.table("intake_log").column("notes").role == "free_text"


def test_zero_one_flags_are_categories(tmp_path):
    from tests.conftest import write_csv
    f = write_csv(tmp_path / "x.csv", ["case_id", "advice_given", "civil"], [[f"K{i}", i % 2, 1] for i in range(10)])
    r = roles(import_files(tmp_path / "x.db", [f]), "x")
    assert r["advice_given"] == "category" and r["civil"] == "category"
