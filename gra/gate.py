"""The privacy gate: validates and runs AI-written SQL, returning only aggregates.

Layers, each sufficient to stop a different class of leak:

1. Parse with sqlglot; one SELECT (CTEs, subqueries and UNIONs allowed) only.
2. Resolve every column to its source table column and its privacy role,
   following aliases through CTEs and subqueries ("taint").
3. Identifier columns may only be used to join, count, partition or test for
   blanks. They can never be output, compared with typed-in values, or passed
   to other functions.
4. The outermost SELECT must aggregate. Its output may contain counts, sums,
   categories and bucketed dates or numbers, never identifiers or raw
   sensitive values.
5. Execution on a read-only connection with an SQLite authorizer that allows
   only reads of permitted columns and allow-listed functions.
6. Result checks: row cap, bucket cardinality cap, and a backstop that refuses
   any result cell equal to a stored identifier or free-text value.
7. Errors are reduced to messages that name only schema objects or query text.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from .importer import Catalog

MAX_ROWS = 500
MAX_BUCKETS = 120
TIMEOUT_SECONDS = 15

ALLOWED_FUNCTIONS = {
    "count", "sum", "avg", "total", "min", "max", "coalesce", "ifnull", "nullif", "iif",
    "strftime", "date", "julianday", "datetime", "unixepoch", "substr", "substring", "lower", "upper",
    "trim", "ltrim", "rtrim", "length", "instr", "replace", "abs", "round", "floor", "ceil", "ceiling",
    "trunc", "sign", "like", "glob", "typeof", "row_number", "rank", "dense_rank", "ntile",
    "lag", "lead", "first_value", "last_value", "cume_dist", "percent_rank", "cast",
}
_DENIED_FUNCTIONS = {
    "group_concat", "string_agg", "array_agg", "json_group_array", "json_group_object", "load_extension",
    "readfile", "writefile", "edit", "fts3_tokenizer", "hex", "unhex", "quote", "char", "printf", "format",
    "randomblob", "zeroblob", "sqlite_version", "sqlite_source_id", "sqlite_compileoption_get",
}

_SAFE_SQLITE_ERRORS = re.compile(
    r"^(no such column|no such table|no such function|ambiguous column name|misuse of (aggregate|window)|"
    r"near \"[^\"]*\": syntax error|wrong number of arguments|.*must appear in the GROUP BY|"
    r"a GROUP BY clause is required|incomplete input|unrecognized token)",
    re.I,
)

_TOKEN = re.compile(r"[A-Za-z0-9_@.+#/:\-]+")
OUTPUT_FORBIDDEN = {"identifier", "free_text"}
NEEDS_BUCKET = {"date", "measure", "text", "sensitive"}


class QueryRefused(Exception):
    """A refusal whose message is safe to show the AI and the user."""


@dataclass
class QueryResult:
    query_id: str
    sql: str
    purpose: str
    columns: list[str]
    rows: list[list]
    elapsed_ms: int = 0


@dataclass
class _Taint:
    roles: set = field(default_factory=set)
    derived: bool = False  # passed through a function or operator
    aggregated: bool = False  # produced by COUNT/SUM/AVG/TOTAL
    minmax: bool = False

    def merge(self, other: "_Taint") -> "_Taint":
        return _Taint(self.roles | other.roles, self.derived or other.derived,
                      self.aggregated or other.aggregated, self.minmax or other.minmax)


class QueryGate:
    def __init__(self, db_path: Path, catalog: Catalog, *, max_rows: int = MAX_ROWS,
                 max_buckets: int = MAX_BUCKETS, timeout: float = TIMEOUT_SECONDS):
        self.db_path = Path(db_path)
        self.catalog = catalog
        self.max_rows = max_rows
        self.max_buckets = max_buckets
        self.timeout = timeout
        self.log: list[QueryResult] = []
        self._schema = {
            t.name: {c.name: c.sql_type for c in t.columns if c.role != "free_text"}
            for t in catalog.tables
        }
        self._roles = {(t.name, c.name): c.role for t in catalog.tables for c in t.columns}
        self._hidden_columns = {(t, c) for (t, c), r in self._roles.items() if r == "free_text"}
        self._protected_values = self._load_values(("identifier", "free_text"))
        # Result cells may never equal a stored personal value, even after case or
        # spacing changes (for example lower(client)): identifiers, notes, birth
        # dates and other unlisted text.
        self._result_blocked = ({_norm(v) for v in self._protected_values}
                                | {_norm(v) for v in self._load_values(("sensitive",))}
                                | self._rare_text_values())
        self._scope_by_select: dict[int, Scope] = {}

    # ------------------------------------------------------------ public

    def run(self, sql: str, purpose: str = "") -> QueryResult:
        safe_sql, bucket_cols = self.check(sql)
        columns, rows, elapsed = self._execute(safe_sql)
        self._check_result(columns, rows, bucket_cols)
        result = QueryResult(f"Q{len(self.log) + 1}", safe_sql, purpose, columns, rows, elapsed)
        self.log.append(result)
        return result

    def check(self, sql: str) -> tuple[str, set[int]]:
        """Validate a query. Returns the SQL to run and the output positions needing a bucket check."""
        if not isinstance(sql, str) or not sql.strip():
            raise QueryRefused("The query is empty.")
        if len(sql) > 20000:
            raise QueryRefused("The query is too long.")
        try:
            statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
        except ParseError as e:
            raise QueryRefused(f"SQL syntax error: {_first_line(e)}") from None
        if len(statements) != 1:
            raise QueryRefused("Send exactly one SELECT statement.")
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise QueryRefused("Only SELECT queries are allowed.")
        for node in tree.walk():
            if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command,
                                 exp.Pragma, exp.Attach, exp.Detach, exp.Into)):
                raise QueryRefused("Only read-only SELECT queries are allowed.")
            if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
                raise QueryRefused("SELECT * is not allowed; name the aggregates you need.")
            if isinstance(node, exp.Func):
                self._check_function(node)
        for table in tree.find_all(exp.Table):
            if table.name.lower().startswith(("sqlite_", "_gra")):
                raise QueryRefused(f"Table {table.name} is not available.")
        try:
            tree = qualify(tree, schema=self._schema, dialect="sqlite", validate_qualify_columns=True,
                           quote_identifiers=True, identify=True)
        except (OptimizeError, SqlglotError) as e:
            raise QueryRefused(f"Could not resolve the query against the data: {_first_line(e)}") from None

        root = build_scope(tree)
        if root is None:
            raise QueryRefused("Could not analyse the query.")
        self._scope_by_select = {id(sc.expression): sc for sc in root.traverse()}
        taints: dict[int, dict[str, _Taint]] = {}
        for scope in root.traverse():
            taints[id(scope)] = self._scope_outputs(scope, taints)

        finals = _final_selects(tree)
        for scope in root.traverse():
            self._check_identifier_uses(scope, taints, finals)

        bucket_cols: set[int] = set()
        for select in finals:
            scope = _scope_for(root, select)
            bucket_cols |= self._check_final(scope, taints, select)
        return tree.sql(dialect="sqlite"), bucket_cols

    # ------------------------------------------------------------ analysis

    def _check_function(self, node: exp.Func):
        # Operators that sqlglot models as functions.
        if isinstance(node, (exp.Binary, exp.Connector, exp.Predicate, exp.Unary, exp.Exists, exp.Case, exp.If,
                             exp.Cast, exp.Window, exp.Distinct)):
            return
        name = node.name.lower() if isinstance(node, exp.Anonymous) else node.sql_name().lower()
        if name in _DENIED_FUNCTIONS or isinstance(node, exp.GroupConcat):
            raise QueryRefused(f"Function {name.upper()} is not allowed.")
        # Functions sqlglot does not know are checked by name here; known ones are
        # checked by name again by the SQLite authorizer at run time.
        if isinstance(node, exp.Anonymous) and name not in ALLOWED_FUNCTIONS:
            raise QueryRefused(f"Function {name.upper()} is not allowed.")

    def _column_taint(self, col: exp.Column, scope: Scope, taints) -> _Taint:
        # Resolve from the scope of the SELECT that actually contains the column.
        owner = col.find_ancestor(exp.Select)
        s = self._scope_by_select.get(id(owner), scope) if owner is not None else scope
        while s is not None:
            source = s.sources.get(col.table)
            if source is not None:
                if isinstance(source, exp.Table):
                    role = self._roles.get((source.name.lower(), col.name.lower()))
                    if role is None or (source.name.lower(), col.name.lower()) in self._hidden_columns:
                        raise QueryRefused(f"Column {col.name} is not available.")
                    return _Taint({role})
                if isinstance(source, Scope):
                    out = taints.get(id(source), {})
                    if col.name in out:
                        return out[col.name]
                    raise QueryRefused(f"Column {col.name} could not be resolved.")
            s = s.parent
        raise QueryRefused(f"Column {col.sql()} could not be resolved.")

    def _expr_taint(self, node: exp.Expression, scope: Scope, taints) -> _Taint:
        if isinstance(node, exp.Column):
            return self._column_taint(node, scope, taints)
        if isinstance(node, (exp.Literal, exp.Null, exp.Boolean, exp.Star)):
            return _Taint()
        if isinstance(node, (exp.Subquery, exp.Select, exp.Union)):
            child = _child_scope(scope, node)
            t = _Taint()
            if child is not None:
                for v in taints.get(id(child), {}).values():
                    t = t.merge(v)
            return t
        if isinstance(node, exp.Count):
            return _Taint(aggregated=True)
        if isinstance(node, (exp.Sum, exp.Avg)) or (isinstance(node, exp.Anonymous) and node.name.lower() == "total"):
            return _Taint(aggregated=True)
        if isinstance(node, exp.Window):
            inner = node.this
            if isinstance(inner, (exp.RowNumber,)) or (isinstance(inner, exp.Anonymous) and inner.name.lower() in
                                                       {"rank", "dense_rank", "ntile", "cume_dist", "percent_rank"}):
                return _Taint({"measure"}, derived=True)
            return self._expr_taint(inner, scope, taints)
        if isinstance(node, (exp.Min, exp.Max)) and not node.expressions:
            t = self._expr_taint(node.this, scope, taints)
            return _Taint(set(t.roles), t.derived, t.aggregated, True)
        if isinstance(node, exp.Case):
            t = _Taint()
            for branch in node.args.get("ifs") or []:
                t = t.merge(self._expr_taint(branch.args["true"], scope, taints))
            if node.args.get("default") is not None:
                t = t.merge(self._expr_taint(node.args["default"], scope, taints))
            return _Taint(t.roles, True, t.aggregated, t.minmax) if t.roles else t
        if isinstance(node, exp.If):
            t = self._expr_taint(node.args["true"], scope, taints)
            if node.args.get("false") is not None:
                t = t.merge(self._expr_taint(node.args["false"], scope, taints))
            return _Taint(t.roles, True, t.aggregated, t.minmax) if t.roles else t
        if isinstance(node, exp.Alias):
            return self._expr_taint(node.this, scope, taints)
        if isinstance(node, (exp.Paren,)):
            return self._expr_taint(node.this, scope, taints)
        t = _Taint()
        for child in node.iter_expressions():
            t = t.merge(self._expr_taint(child, scope, taints))
        if t.roles:
            t = _Taint(t.roles, True, t.aggregated, t.minmax)
        return t

    def _scope_outputs(self, scope: Scope, taints) -> dict[str, _Taint]:
        expr = scope.expression
        out: dict[str, _Taint] = {}
        if isinstance(expr, exp.Select):
            for proj in expr.expressions:
                out[proj.alias_or_name] = self._expr_taint(proj, scope, taints)
        elif isinstance(expr, (exp.Union, exp.Intersect, exp.Except)):
            for child in _set_children(scope):
                for i, (name, t) in enumerate(taints.get(id(child), {}).items()):
                    key = list(out)[i] if i < len(out) else name
                    out[key] = out[key].merge(t) if key in out else t
        return out

    def _check_identifier_uses(self, scope: Scope, taints, finals):
        expr = scope.expression
        if not isinstance(expr, exp.Select):
            return
        for col in scope.columns:
            taint = self._column_taint(col, scope, taints)
            if "identifier" not in taint.roles or taint.aggregated:
                continue
            if not self._identifier_use_ok(col, scope, taints, any(expr is f for f in finals)):
                raise QueryRefused(
                    f"Identifier column {col.name} can only be used to join records, count them "
                    f"(COUNT or COUNT(DISTINCT ...)), partition window functions, or test IS NULL.")

    def _identifier_use_ok(self, col: exp.Column, scope: Scope, taints, is_final: bool) -> bool:
        parent = col.parent
        if isinstance(parent, exp.Distinct):
            parent = parent.parent
        if isinstance(parent, exp.Count):
            return True
        if isinstance(parent, exp.Is):
            return isinstance(parent.expression, exp.Null)
        if isinstance(parent, (exp.EQ, exp.NEQ)):
            other = parent.expression if parent.this is col else parent.this
            return isinstance(other, exp.Column) and "identifier" in self._column_taint(other, scope, taints).roles
        if isinstance(parent, exp.In):
            return parent.this is col and parent.args.get("query") is not None
        if isinstance(parent, exp.Window):
            return col in (parent.args.get("partition_by") or [])
        if isinstance(parent, exp.Ordered):
            return isinstance(parent.parent, exp.Order) and (
                isinstance(parent.parent.parent, exp.Window) or not is_final)
        if is_final:
            return False
        if isinstance(parent, exp.Group):
            return True
        if isinstance(parent, exp.Alias):
            parent = parent.parent
        return isinstance(parent, exp.Select) and col in parent.expressions or (
            isinstance(parent, exp.Select) and any(col is p or col is p.this for p in parent.expressions))

    def _check_final(self, scope: Scope, taints, select: exp.Select) -> set[int]:
        aggregate = bool(select.args.get("group")) or any(
            _has_aggregate(p) for p in select.expressions)
        if not aggregate and not self._only_aggregated_sources(scope, taints):
            raise QueryRefused(
                "The outermost SELECT must return aggregates (COUNT, SUM, AVG) grouped by "
                "categories or buckets, not individual records.")
        group_sqls = {g.sql() for g in (select.args.get("group").expressions if select.args.get("group") else [])}
        bucket: set[int] = set()
        for i, proj in enumerate(select.expressions):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if aggregate:
                for col in _bare_columns(inner):
                    if col.sql() not in group_sqls and inner.sql() not in group_sqls:
                        raise QueryRefused(
                            f"Output column {proj.alias_or_name} mixes a raw column with aggregates; "
                            "add it to GROUP BY or aggregate it.")
            t = self._expr_taint(proj, scope, taints)
            if t.roles & OUTPUT_FORBIDDEN:
                raise QueryRefused(f"Output column {proj.alias_or_name} would reveal identifier values.")
            if t.minmax and t.roles & {"sensitive", "identifier", "free_text", "text"}:
                raise QueryRefused(f"MIN/MAX of {proj.alias_or_name} would reveal an individual value.")
            if "sensitive" in t.roles and not t.derived:
                raise QueryRefused(f"Output column {proj.alias_or_name} would reveal raw personal values; "
                                   "bucket it (for example into age bands).")
            if t.roles & NEEDS_BUCKET:
                if not t.derived and not t.aggregated and not t.minmax:
                    raise QueryRefused(
                        f"Output column {proj.alias_or_name} lists raw values; bucket it (for example "
                        "strftime('%Y', date), CASE ranges, or age bands).")
                bucket.add(i)
        return bucket

    def _only_aggregated_sources(self, scope: Scope, taints) -> bool:
        """A non-aggregating outer SELECT is fine if it only reads safe aggregated subqueries."""
        if not scope.sources:
            return False
        for source in scope.sources.values():
            if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
                return False
            sel = source.expression
            if not (sel.args.get("group") or any(_has_aggregate(p) for p in sel.expressions)):
                if not self._only_aggregated_sources(source, taints):
                    return False
            if sel.args.get("group"):
                for g in sel.args["group"].expressions:
                    t = self._expr_taint(g, source, taints)
                    if t.roles & OUTPUT_FORBIDDEN or ("sensitive" in t.roles and not t.derived):
                        return False
        return True

    # ------------------------------------------------------------ execution

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
        hidden = self._hidden_columns

        def authorizer(action, arg1, arg2, dbname, source):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                table = (arg1 or "").lower()
                if table.startswith(("sqlite_", "_gra")) or (table, (arg2 or "").lower()) in hidden:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK if (arg2 or "").lower() in ALLOWED_FUNCTIONS else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        return conn

    def _execute(self, sql: str):
        conn = self._connect()
        deadline = time.monotonic() + self.timeout
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        start = time.monotonic()
        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(self.max_rows + 1)
        except sqlite3.Error as e:
            raise QueryRefused(_safe_sqlite_error(e)) from None
        except MemoryError:
            # SQLite reports running out of memory this way (for example a join that multiplies rows).
            raise QueryRefused("The query needed more memory than is available. Simplify it: join on keys, "
                               "filter earlier, or aggregate in steps.") from None
        finally:
            conn.close()
        if len(rows) > self.max_rows:
            raise QueryRefused(f"The result has more than {self.max_rows} rows; aggregate into fewer groups.")
        return columns, [list(r) for r in rows], int((time.monotonic() - start) * 1000)

    def _check_result(self, columns, rows, bucket_cols: set[int]):
        for i in bucket_cols:
            distinct = {r[i] for r in rows}
            if len(distinct) > self.max_buckets:
                raise QueryRefused(
                    f"Output column {columns[i]} has {len(distinct)} distinct values; group it into at most "
                    f"{self.max_buckets} buckets (for example by year, month or ranges).")
        for r in rows:
            for v in r:
                if isinstance(v, str) and _norm(v) in self._result_blocked:
                    raise QueryRefused("The result would reveal individual identifier, personal or note values.")

    def protected_value_count(self) -> int:
        return len(self._protected_values)

    def contains_protected(self, text: str) -> bool:
        """Whether text contains a stored identifier or note value."""
        return bool(self.protected_matches(text, limit=1))

    def protected_matches(self, text: str, limit: int = 3) -> list[str]:
        """Stored identifier or note values found in text (at most limit of them)."""
        tokens = set(_TOKEN.findall(text))
        found: list[str] = []
        for value in sorted(self._protected_values):
            if (value in tokens) if _TOKEN.fullmatch(value) else (value in text):
                found.append(value)
                if len(found) >= limit:
                    break
        return found

    def _rare_text_values(self) -> set[str]:
        """Unlisted text values held by only a few records (a household or a name that was not
        recognized as one). Common values, such as a status in a change log, stay usable."""
        common: set[str] = set()
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        try:
            for (table, col), role in self._roles.items():
                if role == "category":
                    common |= {_norm(v) for (v,) in conn.execute(
                        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL')}
            rare: set[str] = set()
            for (table, col), role in self._roles.items():
                if role != "text":
                    continue
                for v, n in conn.execute(f'SELECT "{col}", COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL '
                                         f'GROUP BY 1 HAVING COUNT(*) <= {RARE_TEXT_MAX_ROWS}'):
                    s = str(v)
                    if len(s) >= 4 and not s.replace(".", "").isdigit() and _norm(s) not in common:
                        rare.add(_norm(s))
        finally:
            conn.close()
        return rare

    def _load_values(self, roles: tuple[str, ...]) -> set[str]:
        values: set[str] = set()
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        try:
            for (table, col), role in self._roles.items():
                if role not in roles:
                    continue
                for (v,) in conn.execute(f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'):
                    s = str(v)
                    if len(s) >= 4 and not s.replace(".", "").isdigit():
                        values.add(s)
        finally:
            conn.close()
        return values


# ---------------------------------------------------------------- helpers

RARE_TEXT_MAX_ROWS = 3

def _norm(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__


def _safe_sqlite_error(e: sqlite3.Error) -> str:
    msg = str(e)
    if "interrupted" in msg:
        return "The query took too long and was stopped; simplify it."
    if "not authorized" in msg or "prohibited" in msg:
        return "The query uses an operation or column that is not allowed."
    if _SAFE_SQLITE_ERRORS.match(msg):
        return f"Query failed: {msg[:300]}"
    return "The query failed to run."


def _has_aggregate(node: exp.Expression) -> bool:
    for n in node.walk():
        if isinstance(n, (exp.Subquery, exp.Select)) and n is not node:
            continue
        if isinstance(n, exp.AggFunc) and not isinstance(n.parent, exp.Window):
            if _inside_subquery(n, node):
                continue
            return True
        if isinstance(n, exp.Anonymous) and n.name.lower() == "total":
            return True
    return False


def _inside_subquery(n: exp.Expression, root: exp.Expression) -> bool:
    p = n.parent
    while p is not None and p is not root:
        if isinstance(p, (exp.Subquery, exp.Select)):
            return True
        p = p.parent
    return False


def _bare_columns(node: exp.Expression):
    """Columns not inside an aggregate, window or subquery."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, exp.Column):
            yield n
            continue
        if isinstance(n, (exp.AggFunc, exp.Window, exp.Subquery, exp.Select)) or (
                isinstance(n, exp.Anonymous) and n.name.lower() == "total"):
            continue
        stack.extend(n.iter_expressions())


def _final_selects(tree: exp.Expression) -> list[exp.Select]:
    if isinstance(tree, exp.Select):
        return [tree]
    if isinstance(tree, (exp.Union, exp.Intersect, exp.Except)):
        return _final_selects(tree.this) + _final_selects(tree.expression)
    if isinstance(tree, exp.Subquery):
        return _final_selects(tree.this)
    return []


def _scope_for(root: Scope, select: exp.Select) -> Scope:
    for s in root.traverse():
        if s.expression is select:
            return s
    raise QueryRefused("Could not analyse the query.")


def _child_scope(scope: Scope, node: exp.Expression) -> Scope | None:
    target = node.this if isinstance(node, exp.Subquery) else node
    s = scope
    while s is not None:
        for child in s.subquery_scopes + s.derived_table_scopes + _set_children(s) + s.cte_scopes:
            if child.expression is target:
                return child
        s = s.parent
    return None


def _set_children(scope: Scope) -> list:
    return list(getattr(scope, "set_operation_scopes", None) or getattr(scope, "union_scopes", None) or [])
