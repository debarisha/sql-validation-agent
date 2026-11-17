# SQL Validation Agent

Checks that a **target table matches its source table** after a migration or data load, on SQL Server.
You name the two tables. The agent generates five validation queries, runs them **read-only**, and returns a report
with a PASS/FAIL verdict and the evidence behind it.

```
/start  →  sql-generator agent  →  mapping (.yaml)  →  5 SQL checks  →  read-only run  →  HTML / Excel / JSON report
```

---

## Quick start

In Claude Code, from this folder:

| Command | What happens |
|---|---|
| `/start` | Guided. Pick a saved mapping or set up a new table pair, then choose the server and database. |
| `/start invoice` | Re-runs the saved `invoice` mapping. It only asks where to run. |
| `/start invoice -S localhost -d ValidationSandbox` | Runs straight away with no questions. |

For a **new table pair**, `/start` asks for:

1. Source and target tables (`schema.table`)
2. Key column(s), for example `OrderID` or `OrderID + LineNo`
3. Any column renamed in the target (`SrcName → TgtName`)
4. The column list. `/start` gives you an `INFORMATION_SCHEMA` query to run in SSMS and paste back.
5. The server and database to run on, or "generate only"

Try new mappings on a small table or in `ValidationSandbox` first.

---

## The five checks

| ID | File | Checks | Severity | Passes when |
|---|---|---|---|---|
| VC-01 | `01_row_count.sql` | Total rows, source vs target | High | `row_count_diff = 0` |
| VC-02 | `02_null_checks.sql` | NULLs per column on each side; required columns flagged | High | 0 NULLs in required columns |
| VC-03 | `03_duplicate_checks.sql` | Keys that appear more than once, per side | Critical | 0 rows |
| VC-04 | `04_missing_extra.sql` | Keys on only one side (`MISSING_IN_TARGET` / `EXTRA_IN_TARGET`) | Critical | 0 rows |
| VC-05 | `05_column_compare.sql` | Values that differ for keys present on both sides | High | 0 rows |

- Composite keys and renamed target columns are supported.
- Comparisons are NULL-safe: NULL = NULL is a match, and NULL vs a value is a difference.

---

## Inputs and outputs

**Input:** one mapping file per table pair, `mappings/<name>.yaml`. The agent writes it for you.

```yaml
table: invoice                 # output folder name
dialect: sqlserver
source: src.Invoice
target: tgt.Invoice
key: [InvoiceID]               # composite keys: [OrderID, LineNo]
columns:
  - { name: InvoiceID,  type: "int",           required: true,  compare: false }  # key
  - { name: CustomerNo, type: "varchar(20)",   required: true,  compare: true }
  - { name: Amount,     type: "decimal(18,2)", required: true,  compare: true }
  - { name: UpdatedAt,  type: "datetime2(3)",  required: false, compare: false }  # not compared
column_map:                    # only when a target column name differs
  CustomerNo: CustomerNumber
```

Always quote `type` values. A type such as `decimal(18,2)` contains a comma, and the generator rejects it unquoted.

**Outputs:**

| Output | Location |
|---|---|
| Generated SQL (5 files) | `generated_sql/<table>/` |
| HTML report: verdict, metrics, schema comparison, evidence, executed SQL | `reports/<table>/<timestamp>/report.html` |
| Excel workbook: one sheet per check, every failing row | `reports/<table>/<timestamp>/results.xlsx` |
| JSON summary, for automation | `reports/<table>/<timestamp>/summary.json` |
| Structured report in the chat: findings, likely causes, next actions | the `/start` reply |

---

## Running without the agent

```bash
# Generate the SQL only (no database access)
python generator/generate_sql.py mappings/invoice.yaml
python generator/generate_sql.py --all

# Generate + run + report (read-only)
python runner/run_validation.py mappings/invoice.yaml -S localhost -d ValidationSandbox
```

Runner options:

| Option | Meaning |
|---|---|
| `-S`, `--server` | SQL Server (default `localhost`) |
| `-d`, `--database` | Database (required) |
| `--user` | SQL login. The password is read from the `MSSQL_PASSWORD` environment variable. Without `--user`, Windows authentication is used. |
| `--driver` | ODBC driver name (default: newest installed) |
| `--out` | Report folder (default `reports/`) |
| `--max-rows` | Evidence rows per check in the HTML report (default 200; Excel always has all rows) |

**Exit codes:** `0` = all checks PASS · `1` = at least one FAIL · `2` = ERROR (connection failure, missing column or SQL error)

You can also open any file in `generated_sql/<table>/` in SSMS and run it with **F5**.

---

## Project structure

```
sql_validation_generator/
├── .claude/
│   ├── agents/sql-generator.md     # the agent: rules, workflow, report templates
│   └── skills/start/SKILL.md       # the /start command
├── generator/generate_sql.py       # mapping → 5 SQL files (never touches the database)
├── runner/run_validation.py        # pre-flight + read-only run + HTML/Excel/JSON reports
├── mappings/                       # one .yaml per table pair
├── generated_sql/                  # generated checks, one folder per table
├── reports/                        # one folder per run
├── test_data/                      # sandbox test tables + expected results
├── CLAUDE.md                       # project rules for Claude
└── README.md
```

---

## Requirements

- Claude Code (for `/start` and the agent)
- SQL Server 2016 or later
- Python 3 with `pyyaml`, `pyodbc` and `openpyxl`
  ```bash
  pip install pyyaml pyodbc openpyxl
  ```
- ODBC Driver 17 or 18 for SQL Server

---

## Test data

These scripts build tables with planted defects. They run **only** in a database named `ValidationSandbox`.

| Set | Script | Expected results | Size |
|---|---|---|---|
| Contract | `test_data/01_create_contract_test_data.sql` | `test_data/expected_results.md` | 9 vs 10 rows, one defect per check |
| Invoice | `test_data/03_create_invoice_test_data.sql` | `test_data/expected_results_invoice.md` | 5,001 vs 4,989 rows, 13 defects, edge cases, renamed column |

```sql
CREATE DATABASE ValidationSandbox;
-- then, in ValidationSandbox, run a script above
```

`test_data/02_fix_target_to_pass.sql` copies the Contract source into the target, so every check passes.

On the Invoice set, the agent found 13/13 planted defects and 35/35 value mismatches, with 0 false alarms, in about 1 second.

---

## Safety

- **Read-only.** The checks are `SELECT` statements only. The generator never connects to a database.
- **Pre-flight.** Every mapped column is confirmed to exist before any check runs.
- **No guessing.** If no key is given, the agent stops and asks.
- **No invented columns or numbers.** The agent's report quotes only the run's output.
- **Audit trail.** Every report records the server, database, collation, and SHA-256 hashes of the mapping and of each SQL file.

---

## Known limitations

- **SQL Server only.** Snowflake is in the spec but not implemented yet.
- **Collation-dependent text comparison.** On a case- and accent-insensitive collation such as `Latin1_General_CI_AI`, case and accent differences are not flagged. Trailing spaces are always ignored; leading spaces are detected.
- **No numeric tolerance.** Any difference is a mismatch, including float drift in the last digits.
- **No column transforms** in the mapping yet.
- **Large tables not measured.** Tested up to 5,000 rows; very large tables have not been timed.
- **Files must be loaded first.** CSV or Excel data has to be in tables before it can be validated.
- **Empty strings in Excel.** `results.xlsx` shows empty strings as blank cells; `report.html` labels them `(empty string)`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `/start` is not listed | Reload VS Code (Ctrl+Shift+P → **Developer: Reload Window**). |
| `fork: Resource temporarily unavailable`, `0xC0000142` (Git Bash) | Windows Mandatory ASLR is blocking Git Bash. Check with `Get-ProcessMitigation -System`. Exempt the Git executables from `ForceRelocateImages` in an elevated PowerShell: `Set-ProcessMitigation -Name <exe> -Disable ForceRelocateImages`. This may be needed again after a Git upgrade. |
| Run reports `ERROR` with "missing mapped column(s)" | A mapping column name doesn't exist in the database. Fix the mapping and re-run. |
| Generator error "unknown field(s)" | A `type` with a comma is unquoted. Write `type: "decimal(18,2)"`. |
| Accented text shows as `Ã©` | The SQL file was run with the wrong encoding. Use SSMS, or `sqlcmd -f 65001`. |
