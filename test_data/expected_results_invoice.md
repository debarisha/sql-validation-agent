# Expected results: Invoice test data

**Setup**
1. In the `ValidationSandbox` database, run `test_data/03_create_invoice_test_data.sql`.
2. Run the validation:
   `python runner/run_validation.py mappings/invoice.yaml -S localhost -d ValidationSandbox`

**Overall:** FAIL, with all 5 checks failing.

## Data shape
- **Source** (`src.Invoice`): 5,001 rows, InvoiceIDs 1–5000, with InvoiceID 100 duplicated.
- **Target** (`tgt.Invoice`): 4,989 rows.
- **Renamed column:** `CustomerNo` in the source is `CustomerNumber` in the target.
- **Not compared:** `UpdatedAt` is `compare: false`.

## Planted defects (must be reported)

| # | Side | InvoiceID(s) | Defect | Caught by |
|---|---|---|---|---|
| S1 | source | 100 | Duplicate key | VC-01, VC-03 |
| S2 | source | 200 | Required `CustomerNo` is NULL | VC-02, VC-05 |
| T1 | target | 4976–5000 (25) | Missing from target | VC-01, VC-04 |
| T2 | target | 9001–9010 (10) | Extra, only in target | VC-01, VC-04 |
| T3 | target | 300, 301 | Duplicate keys | VC-01, VC-03 |
| T4 | target | NULL (2 rows) | NULL key | VC-01, VC-02, VC-03 |
| T5 | target | 1001–1020 (20) | `Amount` + 0.01 | VC-05 |
| T6 | target | 1101–1105 (5) | `InvoiceDate` + 1 day | VC-05 |
| T7 | target | 1401 | Leading space in `Status` | VC-05 |
| T8 | target | 1510, 1520, 1530, 1540, 1550 | `Notes` is `''` where the source has NULL | VC-05 |
| T9 | target | 1701 | `ExchangeRate` differs by 1e-15 (float) | VC-05 |
| T10 | target | 1800 | `DueDate` is NULL in the source, filled in the target | VC-05 |
| T11 | target | 1901 | Required `Currency` is NULL | VC-02, VC-05 |

## Edge cases (must NOT be reported)

| # | InvoiceID(s) | Difference | Why it isn't reported |
|---|---|---|---|
| N1 | 1201–1210 | Trailing spaces in `Status` | SQL Server ignores trailing spaces when comparing |
| N2 | 1301–1310 | `Status` in upper case | Case-insensitive collation (`Latin1_General_CI_AI`) |
| N3 | 1601–1603 | `Café` → `Cafe` in `Notes` | Accent-insensitive collation |
| N4 | all rows | `UpdatedAt` differs | `compare: false` in the mapping |
| — | 100, 300, 301 | Duplicated rows | The copies are identical, so VC-05 shows nothing for them |

N1–N3 are by design under the current collation. If the target system treats these as real differences, the
comparison would need a binary or case-sensitive collation. That option isn't implemented yet.

## VC-01 Row count: FAIL

| src_row_count | tgt_row_count | row_count_diff |
|---|---|---|
| 5001 | 4989 | 12 |

The target total breaks down as 5,000 − 25 (T1) + 10 (T2) + 2 (T3) + 2 (T4) = 4,989.

## VC-02 NULL checks: FAIL (3 FAIL rows)

| side | column | required | null_count | status |
|---|---|---|---|---|
| source | CustomerNo | 1 | **1** | **FAIL** |
| source | InvoiceID, InvoiceDate, Amount, Currency | 1 | 0 | PASS |
| source | DueDate | 0 | 251 | INFO |
| source | Notes | 0 | 501 | INFO |
| source | Status, ExchangeRate, UpdatedAt | 0 | 0 | INFO |
| target | InvoiceID | 1 | **2** | **FAIL** |
| target | Currency | 1 | **1** | **FAIL** |
| target | CustomerNo, InvoiceDate, Amount | 1 | 0 | PASS |
| target | DueDate | 0 | 248 | INFO |
| target | Notes | 0 | 496 | INFO |
| target | Status, ExchangeRate, UpdatedAt | 0 | 0 | INFO |

How the INFO counts arise:
- **Source DueDate:** every 20th ID is NULL (250), plus the duplicated row 100 (+1) = 251.
- **Source Notes:** every 10th ID is NULL (500), plus row 100 (+1) = 501.
- **Target DueDate:** 250, then
  - deleted 4980 and 5000 (−2);
  - duplicate 300 (+1);
  - 1800 filled in (−1)

  = 248.
- **Target Notes:** 500, then
  - deleted 4980, 4990 and 5000 (−3);
  - extra row 9010 (+1);
  - duplicate 300 (+1);
  - the two NULL-key copies of 50 (+2);
  - five values changed to `''` (−5)

  = 496.

## VC-03 Duplicate keys: FAIL (4 rows)

| side | InvoiceID | occurrences |
|---|---|---|
| source | 100 | 2 |
| target | NULL | 2 |
| target | 300 | 2 |
| target | 301 | 2 |

The two NULL-key rows are grouped together, so they show up here as a duplicate. They are also reported by VC-02.

## VC-04 Missing / extra: FAIL (35 rows)
- `EXTRA_IN_TARGET`: 9001–9010 (10 rows)
- `MISSING_IN_TARGET`: 4976–5000 (25 rows)

## VC-05 Column compare: FAIL (35 rows, 35 distinct keys)

| Column | Mismatches | InvoiceIDs |
|---|---|---|
| Amount | 20 | 1001–1020 (e.g. 1001: `380.37` → `380.38`) |
| InvoiceDate | 5 | 1101–1105 |
| Notes | 5 | 1510, 1520, 1530, 1540, 1550 (NULL → `''`) |
| CustomerNo | 1 | 200 (NULL → `C00200`) |
| Status | 1 | 1401 (leading space) |
| ExchangeRate | 1 | 1701 |
| DueDate | 1 | 1800 (NULL → `2025-12-31`) |
| Currency | 1 | 1901 (`INR` → NULL) |
| InvoiceDate, Status (N1, N2), Notes (N3) | 0 extra | must not appear |

## Reconciliation metrics

| Metric | Value |
|---|---|
| Source distinct keys / target distinct keys | 5,000 / 4,985 |
| Matched keys | 4,975 |
| Missing / extra | 25 / 10 |
| Keys with ≥ 1 mismatch | 35 |
| Exact-match keys | 4,940 |
| Key coverage | 99.5% |
| Exact-match rate | 98.8% |
| Attribute values compared | 39,800 (4,975 keys × 8 columns) |
