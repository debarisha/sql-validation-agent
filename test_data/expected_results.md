# Expected results: Contract test data

These are the results the five checks in `generated_sql/contract/` should return after you run
`test_data/01_create_contract_test_data.sql` in the `ValidationSandbox` database.
If what you get differs from this, either the data or the SQL is wrong.

## Planted defects

| ContractID | Defect | Caught by |
|---|---|---|
| 3 | Loaded twice into target | 03 |
| 6 | Target `CustomerID` is NULL (a required column) | 02, 05 |
| 7 | `Amount` 100.00 in source, 150.00 in target | 05 |
| 8 | `EndDate` NULL → 2025-07-31, `Status` Pending → Closed | 05 |
| 9 | Missing from target | 01, 04 |
| 11 | Only in target | 01, 04 |
| 2, 5 | NULLs on **both** sides in optional columns. These must **not** be reported (tests NULL-safe comparison) | none |

## 01_row_count: FAIL

| src_row_count | tgt_row_count | row_count_diff | status |
|---|---|---|---|
| 9 | 10 | -1 | FAIL |

## 02_null_checks: FAIL (one required column)

| side | column_name | is_required | null_count | total_rows | status |
|---|---|---|---|---|---|
| source | Amount | 1 | 0 | 9 | PASS |
| source | ContractID | 1 | 0 | 9 | PASS |
| source | CustomerID | 1 | 0 | 9 | PASS |
| source | StartDate | 1 | 0 | 9 | PASS |
| source | EndDate | 0 | 3 | 9 | INFO |
| source | Status | 0 | 1 | 9 | INFO |
| target | Amount | 1 | 0 | 10 | PASS |
| target | ContractID | 1 | 0 | 10 | PASS |
| target | **CustomerID** | 1 | **1** | 10 | **FAIL** |
| target | StartDate | 1 | 0 | 10 | PASS |
| target | EndDate | 0 | 2 | 10 | INFO |
| target | Status | 0 | 1 | 10 | INFO |

## 03_duplicate_checks: FAIL (1 row)

| side | ContractID | occurrences |
|---|---|---|
| target | 3 | 2 |

## 04_missing_extra: FAIL (2 rows)

| ContractID | issue |
|---|---|
| 11 | EXTRA_IN_TARGET |
| 9 | MISSING_IN_TARGET |

## 05_column_compare: FAIL (4 rows)

| ContractID | column_name | src_value | tgt_value |
|---|---|---|---|
| 6 | CustomerID | 106 | NULL |
| 7 | Amount | 100.00 | 150.00 |
| 8 | EndDate | NULL | 2025-07-31 |
| 8 | Status | Pending | Closed |

ContractID 3 joins twice because of the duplicate, but both copies match the source, so it produces no rows.

## After `02_fix_target_to_pass.sql`

| Check | Expected |
|---|---|
| 01 | 9 / 9 / 0 / PASS |
| 02 | Every required column PASS on both sides; EndDate 3 and Status 1 shown as INFO on both sides |
| 03 | No rows |
| 04 | No rows |
| 05 | No rows |
