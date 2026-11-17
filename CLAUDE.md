# Data Validation SQL Generator

Read a mapping file in mappings/ and write validation SQL files into
generated_sql/<mapping name>/. Supported dialects: sqlserver, snowflake.

## Checks to generate (one file each)
1. 01_row_count.sql: source vs target counts, with the difference
2. 02_null_checks.sql: NULL count per column on each side, and rows that break not_null
3. 03_duplicate_checks.sql: duplicate keys in source and target, reported separately
4. 04_missing_extra.sql: keys in source but not target, and in target but not source
5. 05_column_compare.sql: for keys in both, return key, column_name,
   source_value, target_value where they differ

## Rules
- SELECT statements only. Never write INSERT, UPDATE, DELETE, DROP, TRUNCATE or ALTER.
- Compare NULL-safely: NULL = NULL is a match; NULL vs a value is a difference.
  - sqlserver: (a <> b OR (a IS NULL AND b IS NOT NULL) OR (a IS NOT NULL AND b IS NULL))
  - snowflake: a IS DISTINCT FROM b
- Apply a column's transform to the source side first (replace "source." with "s.").
- Cast to the declared type before comparing decimals and dates.
- Support composite keys: join on every key column.
- In 05_column_compare.sql, use one SELECT per column joined with UNION ALL.
  Cast source_value and target_value to VARCHAR so the UNION works.
- Use aliases s (source) and t (target). Use the target column name as the label.
- Start every file with a comment header: table, check name, and what a passing result looks like.
- Return counts and key columns first so results are easy to review.
- Generated SQL and mappings use only table names, column names and rules, never real data values.
  Real values appear only as evidence in runner reports (report.html, results.xlsx).
- The generator never touches a database. The only allowed database access is the read-only runner
  (`python runner/run_validation.py`), and only against a server and database the user names.
  Never run sqlcmd or write ad-hoc SQL against a database.

## Running and reporting
- `python runner/run_validation.py mappings/<name>.yaml -S <server> -d <database>`. Uses Windows auth by default;
  for a SQL login use `--user <login>` and set the MSSQL_PASSWORD environment variable.
- The runner regenerates the SQL, checks that every mapped column exists, and runs the five checks.
  It writes `reports/<table>/<timestamp>/report.html`, `results.xlsx` and `summary.json`.
- Exit codes: 0 = all PASS, 1 = at least one FAIL, 2 = ERROR.
- Agent: `.claude/agents/sql-generator.md` builds the mapping and generates the SQL. When a server and
  database are named, it also runs the runner and returns a structured report. It never guesses a key.
- Sandbox: `test_data/` builds planted-defect tables, and only in a database named `ValidationSandbox`.
  Contract (small, 9 vs 10 rows): `01_create_contract_test_data.sql` + `expected_results.md`.
  Invoice (5,001 vs 4,989 rows, edge cases, renamed column): `03_create_invoice_test_data.sql` +
  `expected_results_invoice.md`, with mapping `mappings/invoice.yaml`.

## Known gaps between this spec and the current generator
- snowflake: not implemented yet; only sqlserver is generated.
- Column transforms: not supported in the mapping yet.
- 05_column_compare uses CROSS APPLY (VALUES ...) with NVARCHAR text comparison, not UNION ALL with
  VARCHAR and casting to the declared type. NULL-safety is done with EXISTS (SELECT a EXCEPT SELECT b),
  which gives the same result as the expression above.
- Output folder is named after the mapping's `table` field, not the mapping file name.

## When done
Reply with a short table: file, what it checks, what a passing result looks like.
List any assumptions. Remind me to test on a small table first.

## /start command
- `/start` asks which mapping to use (or collects a new table pair), plus the server and database.
- `/start <mapping>` reruns an existing mapping.
- `/start <mapping> -S <server> -d <database>` runs with no questions.
- Defined in `.claude/skills/start/SKILL.md`. It hands the work to the `sql-generator` agent and
  relays its report.
