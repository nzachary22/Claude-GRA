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


def test_zero_one_flags_are_categories(tmp_path):
    from tests.conftest import write_csv
    f = write_csv(tmp_path / "x.csv", ["case_id", "advice_given", "civil"], [[f"K{i}", i % 2, 1] for i in range(10)])
    r = roles(import_files(tmp_path / "x.db", [f]), "x")
    assert r["advice_given"] == "category" and r["civil"] == "category"
