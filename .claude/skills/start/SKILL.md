---
name: start
description: Start a source-to-target data validation run with the sql-generator agent. Collects the table pair, key, columns, server and database (or reuses an existing mapping), then returns the validation report.
argument-hint: "[mapping-name] [-S server] [-d database]"
disable-model-invocation: true
---

# /start: run a data validation

Arguments given: `$ARGUMENTS`

You coordinate the run. The `sql-generator` agent does the work. Follow the steps in order and keep
your messages to the user short.

## Step 1. Read the arguments
- `$ARGUMENTS` may contain a mapping name (e.g. `contract`), `-S <server>` and `-d <database>`.
- List the existing mappings: the `*.yaml` files in `mappings/`.

## Step 2. Decide the mode
- **Re-run:** a mapping name was given and `mappings/<name>.yaml` exists → go to step 3.
- **Name given but no file:** treat it as the name for a new table → go to step 4.
- **No name given:** use AskUserQuestion with the question "What do you want to validate?".
  The options are each existing mapping (label = mapping name, description = its source → target,
  read from the YAML) plus "New table pair". If they choose an existing mapping → step 3,
  otherwise → step 4.

## Step 3. Server and database
If `-S` and `-d` were both given, use them. Otherwise ask with a single AskUserQuestion containing
two questions:
- "Which SQL Server?" with options `localhost (Recommended)` and `.\SQLEXPRESS` (a local named instance).
- "Which database?" with options `ValidationSandbox (Recommended)` and `Generate SQL only, don't run`.

The user can type any other value through "Other". Then go to step 5.

## Step 4. New table pair: collect the inputs
Ask the user in one plain message, not AskUserQuestion, for:
1. Source table (`schema.table`) and target table (`schema.table`)
2. Key column(s), e.g. `OrderID` or `OrderID + LineNo`
3. Any column renamed in the target (`SrcName → TgtName`), or "none"
4. Columns: paste the output of this query, run in SSMS against the source database:
   ```sql
   SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE,
          CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
   FROM INFORMATION_SCHEMA.COLUMNS
   WHERE TABLE_SCHEMA + '.' + TABLE_NAME IN ('<source>', '<target>')
   ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION;
   ```
5. Server and database to run on, or "generate only"

Then end your turn and wait for the reply. When it arrives:
- If the key is still missing, ask again and wait. Never guess the key.
- Choose a mapping name: the one given in `$ARGUMENTS`, or otherwise the target table name in lower case
  (e.g. `orderline`). If `mappings/<name>.yaml` already exists, ask before overwriting it.

Then go to step 5.

## Step 5. Hand off to the agent
Launch the `sql-generator` agent (Agent tool, `subagent_type: "sql-generator"`) with a prompt that
contains everything collected, word for word:
- the mapping name; the source, target, key and column renames; the pasted column list
- either "Run on server <S>, database <D>, and give me the validation report (template A)"
  or "Generate the SQL only (template B), do not run"
- for a re-run: "Use the existing mapping mappings/<name>.yaml unchanged."

Tell the user in one line that the run has started. Then wait for the agent's completion
notification. Never predict or summarise results before it arrives.

## Step 6. Deliver
- Relay the agent's report to the user in full, with its headings and tables, without re-summarising
  or changing any numbers.
- If the report came back as "KEY REQUIRED", show it, ask for the key, and when the user answers,
  go back to step 5.
- If an HTML report was produced, open it with PowerShell:
  `Start-Process "<absolute path to report.html>"`.
- End with one line: "Run `/start <mapping-name>` to re-run this validation."

## Rules
- Never run SQL yourself. Only the agent may run the read-only runner.
- Never invent a server, database, key or columns. Ask instead.
- Remind the user to try new mappings on a small table or the sandbox first.
