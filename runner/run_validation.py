"""Run the generated validation SQL against SQL Server and produce a technical validation report.

Input : a mapping file (mappings/<table>.yaml) + a server/database to run against.
Output: reports/<table>/<timestamp>/
          report.html   - technical validation report (run metadata, scope, schema comparison,
                          check register, reconciliation metrics, per-check evidence, executed SQL)
          results.xlsx  - Run Info, Check Register, Schema Comparison, Reconciliation,
                          Column Mismatches, and one evidence sheet per check
          summary.json  - machine-readable results (for CI / other tools)

Usage:
    python runner/run_validation.py mappings/contract.yaml --server localhost --database ValidationSandbox
    python runner/run_validation.py mappings/contract.yaml -S myserver -d MyDb --user etl_reader
        (password is read from the MSSQL_PASSWORD environment variable)

All statements are read-only SELECTs. Nothing is written to the database.
Exit code: 0 = all checks PASS, 1 = at least one FAIL, 2 = ERROR (connection, missing column, SQL error).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import platform
import sys
import time
from decimal import Decimal
from pathlib import Path

import pyodbc
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "generator"))
from generate_sql import GENERATORS, MappingError, generate, load_mapping  # noqa: E402

TOOL_NAME = "sql_validation_generator"
TOOL_VERSION = "1.1.0"


# --------------------------------------------------------------------------- check register

CHECKS = {
    "01_row_count.sql": dict(
        id="VC-01", name="Row count reconciliation", dimension="Completeness", severity="High",
        objective="Confirm the target holds the same number of records as the source.",
        method="COUNT_BIG(*) on source and target; difference = source - target.",
        criterion="row_count_diff = 0"),
    "02_null_checks.sql": dict(
        id="VC-02", name="Mandatory attribute completeness", dimension="Completeness", severity="High",
        objective="Confirm columns declared required in the mapping contain no NULL values on either side.",
        method="Conditional SUM per column on each table, unpivoted via CROSS APPLY (VALUES ...). "
               "Optional columns are profiled for information only.",
        criterion="null_count = 0 for every required column, on both sides"),
    "03_duplicate_checks.sql": dict(
        id="VC-03", name="Primary key uniqueness", dimension="Uniqueness", severity="Critical",
        objective="Confirm each business key occurs at most once in source and in target.",
        method="GROUP BY key columns HAVING COUNT_BIG(*) > 1, evaluated independently per side.",
        criterion="0 rows returned"),
    "04_missing_extra.sql": dict(
        id="VC-04", name="Key reconciliation (missing / extra)", dimension="Consistency", severity="Critical",
        objective="Confirm the set of business keys is identical in source and target.",
        method="FULL OUTER JOIN of DISTINCT non-NULL keys; unmatched source keys = MISSING_IN_TARGET, "
               "unmatched target keys = EXTRA_IN_TARGET.",
        criterion="0 rows returned"),
    "05_column_compare.sql": dict(
        id="VC-05", name="Attribute value reconciliation", dimension="Accuracy", severity="High",
        objective="Confirm every compared attribute holds the same value in source and target for matched keys.",
        method="INNER JOIN on key; attributes unpivoted to (column, src_value, tgt_value) and compared "
               "NULL-safely with EXISTS (SELECT src EXCEPT SELECT tgt).",
        criterion="0 rows returned"),
}

SEMANTICS = [
    ("Key matching", "Equality on all key columns. Rows with a NULL in any key column are excluded from VC-04/VC-05 "
                     "and surface in VC-02."),
    ("NULL handling", "NULL = NULL is treated as a match; NULL vs a value is a mismatch (EXCEPT semantics)."),
    ("Value normalisation", "Values are cast to NVARCHAR before comparison: date/time types use CONVERT style 121 "
                            "(ISO 8601, full precision), float/real use style 3 (17 significant digits), "
                            "other types use CAST to NVARCHAR(MAX)."),
    ("String comparison", "Governed by the database collation (see Run metadata). Trailing spaces are ignored "
                          "per ANSI padding rules; case sensitivity depends on the collation."),
    ("Duplicate keys", "Duplicated keys multiply join output in VC-05; resolve VC-03 findings before interpreting VC-05."),
    ("Isolation", "Statements run under the session default isolation level (READ COMMITTED unless RCSI is on). "
                  "Concurrent loads during the run can produce transient differences."),
]


# --------------------------------------------------------------------------- database

def connect(args) -> tuple[pyodbc.Connection, str]:
    driver = args.driver or next((d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server")
                                  if d in pyodbc.drivers()), "SQL Server")
    parts = [f"DRIVER={{{driver}}}", f"SERVER={args.server}", f"DATABASE={args.database}",
             "TrustServerCertificate=yes", f"APP={TOOL_NAME}"]
    if args.user:
        pwd = os.environ.get("MSSQL_PASSWORD")
        if pwd is None:
            raise SystemExit("ERROR: --user given but MSSQL_PASSWORD environment variable is not set")
        parts += [f"UID={args.user}", f"PWD={pwd}"]
    else:
        parts.append("Trusted_Connection=yes")
    return pyodbc.connect(";".join(parts), timeout=args.login_timeout, autocommit=True), driver


def fetch(conn, sql: str, params=()) -> tuple[list[str], list[tuple]]:
    cur = conn.cursor()
    cur.execute(sql, params)
    while cur.description is None and cur.nextset():
        pass
    if cur.description is None:
        return [], []
    return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]


def _qt(table: str) -> str:
    return ".".join(f"[{p}]" for p in table.split("."))


def _fmt_type(dtype, charlen, prec, scale) -> str:
    if charlen is not None:
        return f"{dtype}({'max' if charlen == -1 else charlen})"
    if dtype in ("decimal", "numeric") and prec is not None:
        return f"{dtype}({prec},{scale})"
    return dtype


def profile_table(conn, table: str, expected_cols: list[str]) -> dict:
    schema, name = table.split(".", 1)
    info = {"table": table, "exists": False, "row_count": None, "columns": [], "missing_columns": []}
    _, rows = fetch(conn, """SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
                                    CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
                             FROM INFORMATION_SCHEMA.COLUMNS
                             WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                             ORDER BY ORDINAL_POSITION""", (schema, name))
    if not rows:
        info["missing_columns"] = list(expected_cols)
        return info
    info["exists"] = True
    info["columns"] = [{"name": r[0], "type": _fmt_type(r[1], r[3], r[4], r[5]), "nullable": r[2] == "YES"}
                       for r in rows]
    present = {r[0].lower() for r in rows}
    info["missing_columns"] = [c for c in expected_cols if c.lower() not in present]
    _, cnt = fetch(conn, f"SELECT COUNT_BIG(*) FROM {_qt(table)}")
    info["row_count"] = int(cnt[0][0])
    return info


def key_metrics(conn, m: dict) -> dict:
    keys = m["key"]
    tk = [m["column_map"].get(k, k) for k in keys]
    s_sel = ", ".join(f"[{k}]" for k in keys)
    t_sel = ", ".join(f"[{t}] AS [{k}]" for k, t in zip(keys, tk))
    s_nn = " AND ".join(f"[{k}] IS NOT NULL" for k in keys)
    t_nn = " AND ".join(f"[{t}] IS NOT NULL" for t in tk)
    join = " AND ".join(f"s.[{k}] = t.[{k}]" for k in keys)
    sql = f"""WITH s AS (SELECT DISTINCT {s_sel} FROM {_qt(m['source'])} WHERE {s_nn}),
     t AS (SELECT DISTINCT {t_sel} FROM {_qt(m['target'])} WHERE {t_nn})
SELECT (SELECT COUNT_BIG(*) FROM s),
       (SELECT COUNT_BIG(*) FROM t),
       (SELECT COUNT_BIG(*) FROM s INNER JOIN t ON {join});"""
    _, r = fetch(conn, sql)
    return {"src_distinct_keys": int(r[0][0]), "tgt_distinct_keys": int(r[0][1]), "matched_keys": int(r[0][2]),
            "sql": sql}


# --------------------------------------------------------------------------- evaluation

def evaluate(fname: str, cols: list[str], rows: list[tuple]) -> tuple[str, int, str]:
    """Return (status, defect_count, observed)."""
    if fname == "01_row_count.sql":
        r = dict(zip(cols, rows[0]))
        diff = int(r["row_count_diff"])
        return ("PASS" if diff == 0 else "FAIL", abs(diff),
                f"source={r['src_row_count']:,}; target={r['tgt_row_count']:,}; diff={diff:+,}")
    if fname == "02_null_checks.sql":
        idx = {c: i for i, c in enumerate(cols)}
        bad = [r for r in rows if r[idx["status"]] == "FAIL"]
        if not bad:
            return "PASS", 0, "0 NULLs in required columns"
        total = sum(int(r[idx["null_count"]]) for r in bad)
        return "FAIL", total, "; ".join(f"{r[idx['side']]}.{r[idx['column_name']]}={int(r[idx['null_count']]):,} NULL"
                                        for r in bad)
    if fname == "03_duplicate_checks.sql":
        if not rows:
            return "PASS", 0, "0 duplicate keys"
        occ = cols.index("occurrences")
        excess = sum(int(r[occ]) - 1 for r in rows)
        return "FAIL", len(rows), f"{len(rows):,} duplicated key(s); {excess:,} surplus row(s)"
    if fname == "04_missing_extra.sql":
        if not rows:
            return "PASS", 0, "0 unmatched keys"
        iss = cols.index("issue")
        miss = sum(r[iss] == "MISSING_IN_TARGET" for r in rows)
        return "FAIL", len(rows), f"missing_in_target={miss:,}; extra_in_target={len(rows) - miss:,}"
    if not rows:
        return "PASS", 0, "0 attribute mismatches"
    return "FAIL", len(rows), f"{len(rows):,} attribute mismatch(es)"


def schema_comparison(m: dict, src: dict, tgt: dict) -> list[dict]:
    s_cols = {c["name"].lower(): c for c in src.get("columns", [])}
    t_cols = {c["name"].lower(): c for c in tgt.get("columns", [])}
    out = []
    for c in m["columns"]:
        sn, tn = c["name"], m["column_map"].get(c["name"], c["name"])
        s, t = s_cols.get(sn.lower()), t_cols.get(tn.lower())
        notes = []
        if not s:
            notes.append("missing in source")
        if not t:
            notes.append("missing in target")
        if s and t and s["type"].lower() != t["type"].lower():
            notes.append("data type differs")
        if c["required"] and ((s and s["nullable"]) or (t and t["nullable"])):
            notes.append("required in mapping but nullable in DB")
        status = "ERROR" if (not s or not t) else ("WARN" if notes else "OK")
        out.append({"source_column": sn, "target_column": tn, "mapping_type": c["type"],
                    "source_type": s["type"] if s else None, "target_type": t["type"] if t else None,
                    "source_nullable": s["nullable"] if s else None, "target_nullable": t["nullable"] if t else None,
                    "is_key": sn in m["key"], "required": bool(c["required"]),
                    "compared": bool(c["compare"]) and sn not in m["key"],
                    "status": status, "notes": "; ".join(notes)})
    return out


def reconciliation(m: dict, km: dict | None, details: dict) -> dict:
    if not km:
        return {}
    nk = len(m["key"])
    cols05, rows05 = details.get("05_column_compare.sql", ([], []))
    mismatched_keys = {tuple(r[:nk]) for r in rows05}
    per_col: dict[str, int] = {}
    if rows05:
        ci = cols05.index("column_name")
        for r in rows05:
            per_col[r[ci]] = per_col.get(r[ci], 0) + 1
    compared = [c["name"] for c in m["columns"] if c["compare"] and c["name"] not in m["key"]]
    matched = km["matched_keys"]
    exact = matched - len(mismatched_keys)
    rec = {
        "src_distinct_keys": km["src_distinct_keys"], "tgt_distinct_keys": km["tgt_distinct_keys"],
        "matched_keys": matched,
        "missing_in_target": km["src_distinct_keys"] - matched,
        "extra_in_target": km["tgt_distinct_keys"] - matched,
        "keys_with_mismatch": len(mismatched_keys),
        "exact_match_keys": exact,
        "key_coverage_pct": _pct(matched, km["src_distinct_keys"]),
        "exact_match_pct": _pct(exact, km["src_distinct_keys"]),
        "attribute_cells_compared": matched * len(compared),
        "attribute_mismatches": len(rows05),
        "column_breakdown": [{"column": c, "mismatches": per_col.get(c, 0),
                              "mismatch_pct_of_matched": _pct(per_col.get(c, 0), matched)} for c in compared],
    }
    return rec


def _pct(n, d):
    return round(100.0 * n / d, 2) if d else None


# --------------------------------------------------------------------------- run

def run(args) -> int:
    mapping_path = args.mapping.resolve()
    started = dt.datetime.now().astimezone()
    t_run = time.perf_counter()
    m = load_mapping(mapping_path)
    generate(mapping_path, ROOT / "generated_sql")          # keep SQL in sync with the mapping
    sql_dir = ROOT / "generated_sql" / str(m["table"]).lower()
    mapping_bytes = mapping_path.read_bytes()

    src_cols = [c["name"] for c in m["columns"]]
    tgt_cols = [m["column_map"].get(c, c) for c in src_cols]

    s = {
        "run_id": f"{str(m['table']).lower()}-{started.strftime('%Y%m%dT%H%M%S')}",
        "tool": f"{TOOL_NAME} {TOOL_VERSION}",
        "table": m["table"], "mapping": m["_path"],
        "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
        "scope": {"source": m["source"], "target": m["target"], "key": m["key"],
                  "required": [c["name"] for c in m["columns"] if c["required"]],
                  "compared": [c["name"] for c in m["columns"] if c["compare"] and c["name"] not in m["key"]],
                  "column_map": m["column_map"]},
        "environment": {"server_requested": args.server, "database": args.database,
                        "client_host": platform.node(), "python": platform.python_version(),
                        "pyodbc": pyodbc.version},
        "started": started.isoformat(timespec="seconds"),
        "input": {}, "schema_comparison": [], "checks": [], "reconciliation": {}, "overall": None,
    }
    details: dict[str, tuple[list[str], list[tuple]]] = {}

    try:
        conn, driver = connect(args)
        s["environment"]["odbc_driver"] = driver
    except pyodbc.Error as e:
        s["overall"] = "ERROR"
        s["error"] = f"Connection failed: {e}"
        return finish(args, m, s, details, sql_dir, t_run)

    with conn:
        _, v = fetch(conn, """SELECT @@SERVERNAME,
                                     CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(30)),
                                     CAST(SERVERPROPERTY('ProductLevel')   AS nvarchar(30)),
                                     CAST(SERVERPROPERTY('Edition')        AS nvarchar(128)),
                                     CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128)),
                                     SUSER_SNAME(),
                                     (SELECT CASE is_read_committed_snapshot_on WHEN 1 THEN 'RCSI on' ELSE 'RCSI off' END
                                      FROM sys.databases WHERE name = DB_NAME())""")
        env = s["environment"]
        (env["server_name"], env["server_version"], env["product_level"], env["edition"],
         env["collation"], env["login"], env["rcsi"]) = v[0]

        s["input"]["source"] = profile_table(conn, m["source"], src_cols)
        s["input"]["target"] = profile_table(conn, m["target"], tgt_cols)
        s["schema_comparison"] = schema_comparison(m, s["input"]["source"], s["input"]["target"])

        preflight = []
        for side in ("source", "target"):
            p = s["input"][side]
            if not p["exists"]:
                preflight.append(f"{side} table {p['table']} not found in database {args.database}")
            elif p["missing_columns"]:
                preflight.append(f"{side} table {p['table']} is missing mapped column(s): "
                                 + ", ".join(p["missing_columns"]))
        s["preflight_errors"] = preflight

        for fname in GENERATORS:
            meta = CHECKS[fname]
            sql = (sql_dir / fname).read_text(encoding="utf-8")
            entry = {"id": meta["id"], "file": fname, "check": meta["name"], "dimension": meta["dimension"],
                     "severity": meta["severity"], "criterion": meta["criterion"],
                     "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest()}
            if preflight:
                entry.update(status="ERROR", defects=None, observed="Not executed: pre-flight validation failed",
                             rows_returned=None, duration_ms=0)
            else:
                t0 = time.perf_counter()
                try:
                    cols, rows = fetch(conn, sql)
                    details[fname] = (cols, rows)
                    status, defects, observed = evaluate(fname, cols, rows)
                    entry.update(status=status, defects=defects, observed=observed, rows_returned=len(rows))
                except pyodbc.Error as e:
                    entry.update(status="ERROR", defects=None, rows_returned=None,
                                 observed=f"SQL error: {e.args[-1] if e.args else e}")
                entry["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            s["checks"].append(entry)

        if not preflight:
            try:
                km = key_metrics(conn, m)
                s["reconciliation"] = reconciliation(m, km, details)
            except pyodbc.Error as e:
                s["reconciliation"] = {"error": str(e)}

    st = {c["status"] for c in s["checks"]}
    s["overall"] = "ERROR" if "ERROR" in st else "FAIL" if "FAIL" in st else "PASS"
    return finish(args, m, s, details, sql_dir, t_run)


# --------------------------------------------------------------------------- outputs

def finish(args, m, s, details, sql_dir, t_run) -> int:
    finished = dt.datetime.now().astimezone()
    s["finished"] = finished.isoformat(timespec="seconds")
    s["duration_s"] = round(time.perf_counter() - t_run, 2)
    out_dir = args.out.resolve() / str(m["table"]).lower() / finished.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "summary.json").write_text(json.dumps(s, indent=2, default=_jsonable), encoding="utf-8")
    write_excel(out_dir / "results.xlsx", s, details)
    (out_dir / "report.html").write_text(render_html(s, details, sql_dir, args.max_rows), encoding="utf-8")

    print(f"\n{s['run_id']}  overall={s['overall']}  ({s['duration_s']}s)")
    if s.get("error"):
        print(f"  {s['error']}")
    for e in s.get("preflight_errors", []):
        print(f"  PRE-FLIGHT: {e}")
    for c in s["checks"]:
        print(f"  {c['id']}  {c['status']:<5}  {c['check']:<38} {c['observed']}")
    print(f"\nReport : {out_dir / 'report.html'}\nExcel  : {out_dir / 'results.xlsx'}\nJSON   : {out_dir / 'summary.json'}")
    return {"PASS": 0, "FAIL": 1}.get(s["overall"], 2)


def _jsonable(v):
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    return str(v)


def _cell(v):
    if v is None:
        return "NULL"
    if isinstance(v, str) and v == "":
        return "(empty string)"
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, bool):
        return "Y" if v else "N"
    return str(v)


# --------------------------------------------------------------------------- excel

FILLS = {"PASS": "C6EFCE", "FAIL": "FFC7CE", "ERROR": "FFEB9C", "INFO": "DDEBF7", "OK": "C6EFCE", "WARN": "FFEB9C",
         "Critical": "F4B6B0", "High": "FCD9B6", "Medium": "FFF2CC"}
HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")


def _sheet(wb, title, headers, rows, status_cols=()):
    ws = wb.create_sheet(title[:31])
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = HDR_FILL, HDR_FONT
        c.alignment = Alignment(vertical="center")
    for r in rows:
        ws.append([v.isoformat() if isinstance(v, dt.time) else v for v in r])
    for name in status_cols:
        if name in headers:
            ci = headers.index(name) + 1
            for row in ws.iter_rows(min_row=2, min_col=ci, max_col=ci):
                for cell in row:
                    if str(cell.value) in FILLS:
                        cell.fill = PatternFill("solid", fgColor=FILLS[str(cell.value)])
    if not rows:
        ws.append(["(no rows returned: PASS)"])
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(10, width + 2), 80)
    return ws


def write_excel(path: Path, s: dict, details: dict) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    env = s["environment"]
    info = [("Run ID", s["run_id"]), ("Overall result", s["overall"]), ("Tool", s["tool"]),
            ("Started", s["started"]), ("Finished", s["finished"]), ("Duration (s)", s["duration_s"]),
            ("Mapping", s["mapping"]), ("Mapping SHA-256", s["mapping_sha256"]),
            ("Source", s["scope"]["source"]), ("Target", s["scope"]["target"]),
            ("Key", ", ".join(s["scope"]["key"])),
            ("Server", env.get("server_name") or env["server_requested"]), ("Database", env["database"]),
            ("SQL Server version", f"{env.get('server_version', '')} {env.get('product_level', '')}".strip()),
            ("Edition", env.get("edition")), ("Collation", env.get("collation")), ("RCSI", env.get("rcsi")),
            ("Login", env.get("login")), ("ODBC driver", env.get("odbc_driver")), ("Client host", env["client_host"]),
            ("Python / pyodbc", f"{env['python']} / {env['pyodbc']}")]
    for e in ([s["error"]] if s.get("error") else []) + s.get("preflight_errors", []):
        info.append(("ERROR", e))
    ws = _sheet(wb, "Run Info", ["Attribute", "Value"], info)
    ws.cell(3, 2).fill = PatternFill("solid", fgColor=FILLS.get(s["overall"], "FFFFFF"))

    _sheet(wb, "Check Register",
           ["Check ID", "Check", "DQ dimension", "Severity", "Pass criterion", "Observed", "Defects",
            "Rows returned", "Duration (ms)", "Status", "SQL file", "SQL SHA-256"],
           [[c["id"], c["check"], c["dimension"], c["severity"], c["criterion"], c["observed"], c.get("defects"),
             c.get("rows_returned"), c.get("duration_ms"), c["status"], c["file"], c["sql_sha256"]]
            for c in s["checks"]], status_cols=("Status", "Severity"))

    _sheet(wb, "Schema Comparison",
           ["Source column", "Target column", "Mapping type", "Source type", "Target type",
            "Source nullable", "Target nullable", "Key", "Required", "Compared", "Status", "Notes"],
           [[r["source_column"], r["target_column"], r["mapping_type"], r["source_type"], r["target_type"],
             _cell(r["source_nullable"]) if r["source_nullable"] is not None else None,
             _cell(r["target_nullable"]) if r["target_nullable"] is not None else None,
             _cell(r["is_key"]), _cell(r["required"]), _cell(r["compared"]), r["status"], r["notes"]]
            for r in s["schema_comparison"]], status_cols=("Status",))

    rec = s.get("reconciliation") or {}
    if rec and "error" not in rec:
        _sheet(wb, "Reconciliation", ["Metric", "Value"], [
            ("Source rows", s["input"]["source"]["row_count"]), ("Target rows", s["input"]["target"]["row_count"]),
            ("Source distinct keys", rec["src_distinct_keys"]), ("Target distinct keys", rec["tgt_distinct_keys"]),
            ("Matched keys", rec["matched_keys"]), ("Missing in target", rec["missing_in_target"]),
            ("Extra in target", rec["extra_in_target"]), ("Keys with >=1 mismatch", rec["keys_with_mismatch"]),
            ("Exact-match keys", rec["exact_match_keys"]), ("Key coverage %", rec["key_coverage_pct"]),
            ("Exact-match %", rec["exact_match_pct"]), ("Attribute cells compared", rec["attribute_cells_compared"]),
            ("Attribute mismatches", rec["attribute_mismatches"])])
        _sheet(wb, "Column Mismatches", ["Column", "Mismatches", "% of matched keys"],
               [[r["column"], r["mismatches"], r["mismatch_pct_of_matched"]] for r in rec["column_breakdown"]])

    for fname, (cols, rows) in details.items():
        _sheet(wb, f"{CHECKS[fname]['id']} Evidence", cols, rows, status_cols=("status",))
    wb.save(path)


# --------------------------------------------------------------------------- html

def render_html(s: dict, details: dict, sql_dir: Path, max_rows: int) -> str:
    e = html.escape
    env = s["environment"]

    def badge(x):
        x = "" if x is None else str(x)
        return f'<span class="badge b-{e(x).lower()}">{e(x)}</span>'

    def grid(cols, rows, limit=None, badge_cols=("status",)):
        shown = rows if limit is None else rows[:limit]
        head = "".join(f"<th>{e(str(c))}</th>" for c in cols)
        body = []
        for r in shown:
            tds = []
            for c, v in zip(cols, r):
                if c in badge_cols:
                    tds.append(f"<td>{badge(v)}</td>")
                elif v is None:
                    tds.append('<td class="null">NULL</td>')
                elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                    tds.append(f'<td class="num">{e(_cell(v))}</td>')
                else:
                    tds.append(f"<td>{e(_cell(v))}</td>")
            body.append("<tr>" + "".join(tds) + "</tr>")
        more = (f'<p class="note">Showing {len(shown):,} of {len(rows):,} rows; full evidence in results.xlsx.</p>'
                if limit is not None and len(rows) > limit else "")
        return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
                f'<tbody>{"".join(body)}</tbody></table></div>{more}')

    def kv(pairs):
        return '<dl class="kv">' + "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"

    checks = s["checks"]
    n_pass = sum(c["status"] == "PASS" for c in checks)
    n_fail = sum(c["status"] == "FAIL" for c in checks)
    n_err = sum(c["status"] == "ERROR" for c in checks)
    rec = s.get("reconciliation") or {}
    src_p, tgt_p = s["input"].get("source", {}), s["input"].get("target", {})

    def fmt(n):
        return "n/a" if n is None else f"{n:,}"

    # 0. verdict sentence
    if s["overall"] == "PASS":
        verdict = "All validation checks passed. Source and target are reconciled within the defined scope."
    elif s["overall"] == "FAIL":
        failed = ", ".join(f'{c["id"]} ({c["severity"]})' for c in checks if c["status"] == "FAIL")
        verdict = f"{n_fail} of {len(checks)} checks failed: {failed}. The target is not reconciled with the source."
    else:
        verdict = "The run did not complete. See the errors below; no reconciliation conclusion can be drawn."

    errors = ""
    errs = ([s["error"]] if s.get("error") else []) + s.get("preflight_errors", [])
    if errs:
        errors = '<div class="callout err"><b>Errors</b><ul>' + "".join(f"<li>{e(x)}</li>" for x in errs) + "</ul></div>"

    tiles = [("Checks passed", f"{n_pass}/{len(checks)}"),
             ("Source rows", fmt(src_p.get("row_count"))), ("Target rows", fmt(tgt_p.get("row_count")))]
    if rec and "error" not in rec:
        tiles += [("Matched keys", fmt(rec["matched_keys"])),
                  ("Missing / extra", f'{rec["missing_in_target"]:,} / {rec["extra_in_target"]:,}'),
                  ("Attribute mismatches", fmt(rec["attribute_mismatches"])),
                  ("Exact-match rate", "n/a" if rec["exact_match_pct"] is None else f'{rec["exact_match_pct"]}%')]
    tiles_html = "".join(f'<div class="tile"><span>{e(k)}</span><b>{e(v)}</b></div>' for k, v in tiles)

    # 1. metadata
    meta = kv([
        ("Run ID", f"<code>{e(s['run_id'])}</code>"), ("Tool", e(s["tool"])),
        ("Started", e(s["started"])), ("Finished", e(s["finished"])), ("Duration", f"{s['duration_s']} s"),
        ("Server", e(str(env.get("server_name") or env["server_requested"]))), ("Database", e(env["database"])),
        ("SQL Server", e(f"{env.get('server_version', '')} {env.get('product_level', '')} · {env.get('edition', '')}")),
        ("Collation", f"<code>{e(str(env.get('collation', '')))}</code>"), ("Isolation", e(str(env.get("rcsi", "")))),
        ("Login", e(str(env.get("login", "")))), ("ODBC driver", e(str(env.get("odbc_driver", "")))),
        ("Mapping", f"<code>{e(s['mapping'])}</code>"),
        ("Mapping SHA-256", f"<code class='hash'>{e(s['mapping_sha256'])}</code>"),
    ])

    # 2. scope
    sc = s["scope"]
    scope = kv([
        ("Source object", f"<code>{e(sc['source'])}</code>"), ("Target object", f"<code>{e(sc['target'])}</code>"),
        ("Business key", f"<code>{e(', '.join(sc['key']))}</code>"),
        ("Required columns", e(", ".join(sc["required"]) or "none")),
        ("Compared columns", e(", ".join(sc["compared"]) or "none")),
        ("Column mapping", e(", ".join(f"{a} → {b}" for a, b in sc["column_map"].items()) or "identity (same names)")),
    ])

    # 3. schema
    sch_cols = ["source_column", "target_column", "mapping_type", "source_type", "target_type",
                "source_nullable", "target_nullable", "is_key", "required", "compared", "status", "notes"]
    sch_rows = [[r[c] if not isinstance(r[c], bool) else ("Y" if r[c] else "N") for c in sch_cols]
                for r in s["schema_comparison"]]
    schema_html = grid(["Source column", "Target column", "Mapping type", "Source type", "Target type",
                        "Src nullable", "Tgt nullable", "Key", "Required", "Compared", "status", "Notes"],
                       sch_rows) if sch_rows else '<p class="note">Schema not available.</p>'

    # 4. register
    reg_rows = [[c["id"], c["check"], c["dimension"], c["severity"], c["criterion"], c["observed"],
                 c.get("defects"), c.get("duration_ms"), c["status"]] for c in checks]
    register = grid(["ID", "Check", "DQ dimension", "severity", "Pass criterion", "Observed", "Defects",
                     "ms", "status"], reg_rows, badge_cols=("status", "severity"))

    # 5. reconciliation
    rec_html = '<p class="note">Not available (checks not executed).</p>'
    if rec and "error" not in rec:
        rec_tbl = grid(["Metric", "Value"], [
            ["Source rows / distinct keys", f'{fmt(src_p.get("row_count"))} / {rec["src_distinct_keys"]:,}'],
            ["Target rows / distinct keys", f'{fmt(tgt_p.get("row_count"))} / {rec["tgt_distinct_keys"]:,}'],
            ["Matched keys", rec["matched_keys"]],
            ["Missing in target (source only)", rec["missing_in_target"]],
            ["Extra in target (target only)", rec["extra_in_target"]],
            ["Matched keys with ≥1 attribute mismatch", rec["keys_with_mismatch"]],
            ["Exact-match keys", rec["exact_match_keys"]],
            ["Key coverage (matched ÷ source keys)", f'{rec["key_coverage_pct"]}%'],
            ["Exact-match rate (exact ÷ source keys)", f'{rec["exact_match_pct"]}%'],
            ["Attribute cells compared", rec["attribute_cells_compared"]],
            ["Attribute mismatches", rec["attribute_mismatches"]],
        ])
        mx = max([r["mismatches"] for r in rec["column_breakdown"]] + [1])
        bars = "".join(
            f'<tr><td><code>{e(r["column"])}</code></td><td class="num">{r["mismatches"]:,}</td>'
            f'<td class="num">{"" if r["mismatch_pct_of_matched"] is None else str(r["mismatch_pct_of_matched"]) + "%"}</td>'
            f'<td class="barcell"><div class="bar" style="width:{100 * r["mismatches"] / mx:.1f}%"></div></td></tr>'
            for r in rec["column_breakdown"])
        rec_html = (f'<div class="two"><div><h3>Key and row metrics</h3>{rec_tbl}</div>'
                    f'<div><h3>Attribute mismatches by column</h3><div class="scroll"><table><thead><tr><th>Column</th>'
                    f'<th>Mismatches</th><th>% of matched</th><th></th></tr></thead><tbody>{bars}</tbody></table></div></div></div>')
    elif rec.get("error"):
        rec_html = f'<p class="note">Reconciliation metrics failed: {e(rec["error"])}</p>'

    # 6. evidence
    sections = []
    for c in checks:
        meta_c = CHECKS[c["file"]]
        body = kv([("Objective", e(meta_c["objective"])), ("Method", e(meta_c["method"])),
                   ("Pass criterion", f"<code>{e(c['criterion'])}</code>"), ("Observed", e(c["observed"])),
                   ("Result", badge(c["status"]) + f' &nbsp;<span class="note">{c.get("rows_returned") if c.get("rows_returned") is not None else "–"} row(s) · {c.get("duration_ms", "–")} ms</span>')])
        if c["file"] in details:
            cols, rows = details[c["file"]]
            body += ("<h4>Evidence</h4>" + grid(cols, rows, max_rows)) if rows else \
                    '<h4>Evidence</h4><p class="note">No rows returned.</p>'
        sql_path = sql_dir / c["file"]
        sql = sql_path.read_text(encoding="utf-8") if sql_path.exists() else ""
        body += (f'<details><summary>Executed SQL: <code>{e(c["file"])}</code> · SHA-256 '
                 f'<code class="hash">{e(c["sql_sha256"][:16])}…</code></summary><pre>{e(sql)}</pre></details>')
        sections.append(f'<section class="check" id="{e(c["id"])}"><h3><span class="cid">{e(c["id"])}</span> '
                        f'{e(c["check"])} {badge(c["severity"])}</h3>{body}</section>')

    semantics = kv([(k, e(v)) for k, v in SEMANTICS])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data Validation Report</title>
<style>
:root {{ --bg:#f5f6f8; --fg:#1b1f24; --muted:#5f6b7a; --card:#fff; --line:#dfe3e8; --head:#1f3864; --accent:#2f5597;
        --pass:#116329; --pass-bg:#d6f5df; --fail:#a40e26; --fail-bg:#fde2e1; --err:#7a4d00; --err-bg:#fff0c2;
        --info:#1f5fa8; --info-bg:#e1ecfa; --crit:#8b1a10; --crit-bg:#f9d3cf; --high:#8a4500; --high-bg:#fde6cf; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --fg:#e6e9ed; --muted:#97a3b0; --card:#1a1e24; --line:#2c333c;
        --head:#9bb7e8; --accent:#8fb0ea; --pass:#7ee2a0; --pass-bg:#16361f; --fail:#ff9a92; --fail-bg:#461a18;
        --err:#ffd479; --err-bg:#3a2f10; --info:#8ab4f8; --info-bg:#1c2b44; --crit:#ffb0a6; --crit-bg:#4a1d18;
        --high:#ffc58a; --high-bg:#42301a; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:13.5px/1.55 "Segoe UI",-apple-system,Roboto,sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:28px 16px 72px; }}
header {{ border-bottom:3px solid var(--head); padding-bottom:14px; margin-bottom:18px }}
.eyebrow {{ text-transform:uppercase; letter-spacing:.08em; font-size:11px; color:var(--muted); font-weight:600 }}
h1 {{ font-size:24px; margin:2px 0 6px; color:var(--head) }}
h2 {{ font-size:16px; margin:30px 0 10px; color:var(--head); border-bottom:1px solid var(--line); padding-bottom:6px }}
h2 .n {{ color:var(--muted); font-weight:500; margin-right:6px }}
h3 {{ font-size:14px; margin:0 0 10px }} h4 {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); margin:14px 0 6px }}
.verdict {{ display:flex; gap:12px; align-items:center; background:var(--card); border:1px solid var(--line);
            border-left:5px solid var(--accent); border-radius:6px; padding:12px 14px; }}
.verdict.v-pass {{ border-left-color:var(--pass) }} .verdict.v-fail {{ border-left-color:var(--fail) }} .verdict.v-error {{ border-left-color:var(--err) }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin:14px 0 }}
.tile {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px }}
.tile span {{ display:block; font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em }}
.tile b {{ font-size:20px; font-variant-numeric:tabular-nums }}
.card, section.check {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:14px 16px; }}
section.check {{ margin-bottom:12px }}
.cid {{ font-family:Consolas,ui-monospace,monospace; color:var(--accent); margin-right:4px }}
.two {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:14px }}
.two > div {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:14px 16px; min-width:0 }}
.kv {{ display:grid; grid-template-columns:minmax(130px,190px) 1fr; gap:4px 14px; margin:0 }}
.kv dt {{ color:var(--muted); font-weight:600 }} .kv dd {{ margin:0; min-width:0; overflow-wrap:anywhere }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:3px; font-size:11px; font-weight:700; letter-spacing:.03em; text-transform:uppercase }}
.b-pass,.b-ok {{ color:var(--pass); background:var(--pass-bg) }} .b-fail {{ color:var(--fail); background:var(--fail-bg) }}
.b-error,.b-warn {{ color:var(--err); background:var(--err-bg) }} .b-info {{ color:var(--info); background:var(--info-bg) }}
.b-critical {{ color:var(--crit); background:var(--crit-bg) }} .b-high {{ color:var(--high); background:var(--high-bg) }}
.b-medium {{ color:var(--err); background:var(--err-bg) }}
.scroll {{ overflow-x:auto }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px }}
th, td {{ text-align:left; padding:6px 9px; border-bottom:1px solid var(--line); white-space:nowrap; vertical-align:top }}
thead th {{ background:var(--bg); color:var(--muted); font-weight:600; font-size:11.5px; text-transform:uppercase; letter-spacing:.03em }}
td.num {{ text-align:right; font-variant-numeric:tabular-nums }} td.null {{ color:var(--muted); font-style:italic }}
td.barcell {{ width:40%; min-width:100px }} .bar {{ height:8px; background:var(--fail); border-radius:2px; min-width:1px }}
code, pre {{ font-family:Consolas,ui-monospace,monospace; font-size:12px }}
code.hash {{ font-size:11px; color:var(--muted) }}
pre {{ background:var(--bg); border:1px solid var(--line); border-radius:4px; padding:12px; overflow-x:auto; margin:8px 0 0 }}
details summary {{ cursor:pointer; color:var(--muted); margin-top:12px }}
.note {{ color:var(--muted); font-size:12px }}
.callout.err {{ background:var(--fail-bg); color:var(--fail); border-radius:6px; padding:10px 14px; margin:12px 0 }}
.toc {{ display:flex; flex-wrap:wrap; gap:6px 16px; font-size:12px; margin-top:10px }} .toc a {{ color:var(--accent); text-decoration:none }}
footer {{ margin-top:36px; color:var(--muted); font-size:11.5px; border-top:1px solid var(--line); padding-top:10px }}
@media print {{ body {{ background:#fff }} details {{ display:block }} details > pre {{ display:block }}
  section.check, .card, .two > div {{ break-inside:avoid }} }}
</style></head><body><main>
<header>
  <div class="eyebrow">Data Validation Report · Source-to-Target Reconciliation</div>
  <h1>{e(sc['source'])} → {e(sc['target'])}</h1>
  <div class="note">Run <code>{e(s['run_id'])}</code> · {e(s['started'])} · {e(str(env.get('server_name') or env['server_requested']))}/{e(env['database'])}</div>
  <nav class="toc"><a href="#summary">1 Summary</a><a href="#metadata">2 Run metadata</a><a href="#scope">3 Scope</a>
  <a href="#schema">4 Schema comparison</a><a href="#register">5 Check register</a><a href="#recon">6 Reconciliation</a>
  <a href="#evidence">7 Evidence</a><a href="#appendix">A Comparison semantics</a></nav>
</header>

<h2 id="summary"><span class="n">1</span>Executive summary</h2>
<div class="verdict v-{e(s['overall']).lower()}">{badge(s['overall'])}<div>{e(verdict)}</div></div>
{errors}
<div class="tiles">{tiles_html}</div>

<h2 id="metadata"><span class="n">2</span>Run metadata</h2>
<div class="card">{meta}</div>

<h2 id="scope"><span class="n">3</span>Validation scope</h2>
<div class="card">{scope}</div>

<h2 id="schema"><span class="n">4</span>Schema comparison</h2>
<div class="card">{schema_html}
<p class="note">Types are read from INFORMATION_SCHEMA.COLUMNS at run time. WARN means a structural difference to review
(it does not fail the run); ERROR means a mapped column is absent, and the checks are not executed.</p></div>

<h2 id="register"><span class="n">5</span>Check register</h2>
<div class="card">{register}</div>

<h2 id="recon"><span class="n">6</span>Reconciliation metrics</h2>
{rec_html}

<h2 id="evidence"><span class="n">7</span>Check evidence</h2>
{"".join(sections)}

<h2 id="appendix"><span class="n">A</span>Comparison semantics and limitations</h2>
<div class="card">{semantics}</div>

<footer>Generated by {e(s['tool'])} on {e(env['client_host'])} (Python {e(env['python'])}, pyodbc {e(env['pyodbc'])}).
All statements are read-only SELECTs. Evidence tables are truncated to {max_rows} rows; complete result sets are in
results.xlsx. Machine-readable output: summary.json.</footer>
</main></body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run validation checks for a mapping and write HTML/Excel/JSON reports.")
    ap.add_argument("mapping", type=Path, help="mapping YAML, e.g. mappings/contract.yaml")
    ap.add_argument("-S", "--server", default="localhost")
    ap.add_argument("-d", "--database", required=True)
    ap.add_argument("--user", help="SQL login (password from MSSQL_PASSWORD). Omit for Windows authentication.")
    ap.add_argument("--driver", help="ODBC driver name (default: newest installed)")
    ap.add_argument("--out", type=Path, default=ROOT / "reports", help="report root folder")
    ap.add_argument("--max-rows", type=int, default=200, help="max evidence rows per check in the HTML report")
    ap.add_argument("--login-timeout", type=int, default=15)
    args = ap.parse_args(argv)
    try:
        return run(args)
    except MappingError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
