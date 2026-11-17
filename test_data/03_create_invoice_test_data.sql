/* 03_create_invoice_test_data.sql  (SQL Server / T-SQL)
   Purpose : Builds SANDBOX tables src.Invoice (5,001 rows) and tgt.Invoice (4,989 rows) with planted
             defects and edge cases. Each defect is placed on fixed InvoiceIDs, so the expected output is exact.
             Mapping : mappings/invoice.yaml
             Expected: test_data/expected_results_invoice.md
   SAFETY  : DROPS and recreates src.Invoice and tgt.Invoice. Runs only in a database named ValidationSandbox.
   Data is deterministic: every value is derived from InvoiceID, so every run produces identical tables.
   Columns are nullable and there is no primary key on purpose, so NULLs and duplicates can be planted. */

IF DB_NAME() <> N'ValidationSandbox'
BEGIN
    DECLARE @db NVARCHAR(128) = DB_NAME();
    RAISERROR(N'Wrong database. Run this only in ValidationSandbox (current: %s).', 16, 1, @db) WITH NOWAIT;
    SET NOEXEC ON;
END;
GO

IF SCHEMA_ID(N'src') IS NULL EXEC (N'CREATE SCHEMA src');
IF SCHEMA_ID(N'tgt') IS NULL EXEC (N'CREATE SCHEMA tgt');
GO

DROP TABLE IF EXISTS src.Invoice;
DROP TABLE IF EXISTS tgt.Invoice;
GO

CREATE TABLE src.Invoice (
    InvoiceID    INT            NULL,
    CustomerNo   VARCHAR(20)    NULL,
    InvoiceDate  DATE           NULL,
    DueDate      DATE           NULL,
    Amount       DECIMAL(18, 2) NULL,
    Currency     CHAR(3)        NULL,
    Status       NVARCHAR(20)   NULL,
    Notes        NVARCHAR(200)  NULL,
    ExchangeRate FLOAT          NULL,
    UpdatedAt    DATETIME2(3)   NULL
);

CREATE TABLE tgt.Invoice (
    InvoiceID      INT            NULL,
    CustomerNumber VARCHAR(20)    NULL,   -- renamed: CustomerNo in the source
    InvoiceDate    DATE           NULL,
    DueDate        DATE           NULL,
    Amount         DECIMAL(18, 2) NULL,
    Currency       CHAR(3)        NULL,
    Status         NVARCHAR(20)   NULL,
    Notes          NVARCHAR(200)  NULL,
    ExchangeRate   FLOAT          NULL,
    UpdatedAt      DATETIME2(3)   NULL
);
GO

/* ---------------- Base data: InvoiceID 1..5000, identical on both sides ---------------- */
WITH n AS (
    SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS id
    FROM sys.all_objects a CROSS JOIN sys.all_objects b
)
INSERT INTO src.Invoice (InvoiceID, CustomerNo, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt)
SELECT
    id,
    'C' + RIGHT('00000' + CAST(id % 400 AS VARCHAR(5)), 5),
    DATEADD(DAY, id % 365, '2025-01-01'),
    CASE WHEN id % 20 = 0 THEN NULL ELSE DATEADD(DAY, id % 365 + 30, '2025-01-01') END,   -- 250 NULLs (optional)
    CAST((id * 37) % 100000 / 100.0 + 10 AS DECIMAL(18, 2)),
    CASE id % 3 WHEN 0 THEN 'EUR' WHEN 1 THEN 'USD' ELSE 'INR' END,
    CASE id % 4 WHEN 0 THEN N'Paid' WHEN 1 THEN N'Open' WHEN 2 THEN N'Overdue' ELSE N'Cancelled' END,
    CASE WHEN id % 10 = 0 THEN NULL
         ELSE N'Caf' + NCHAR(233) + N' invoice ' + CAST(id AS NVARCHAR(10)) END,   -- 'Café invoice n'; 500 NULLs (optional)
                                                                                     -- NCHAR(233) = é, so the file encoding can't corrupt it
    1.0E0 + id / 1000000.0E0,
    CAST('2025-06-01T00:00:00' AS DATETIME2(3))
FROM n;

INSERT INTO tgt.Invoice (InvoiceID, CustomerNumber, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt)
SELECT InvoiceID, CustomerNo, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt
FROM src.Invoice;
GO

/* ================= SOURCE defects ================= */
-- S1  Duplicate key in SOURCE: InvoiceID 100 twice (identical copy)                       -> VC-01, VC-03
INSERT INTO src.Invoice SELECT * FROM src.Invoice WHERE InvoiceID = 100;
-- S2  Required column NULL in SOURCE: CustomerNo for InvoiceID 200                        -> VC-02, VC-05
UPDATE src.Invoice SET CustomerNo = NULL WHERE InvoiceID = 200;

/* ================= TARGET defects ================= */
-- T1  25 keys missing from target: 4976..5000                                             -> VC-01, VC-04
DELETE FROM tgt.Invoice WHERE InvoiceID BETWEEN 4976 AND 5000;
-- T2  10 extra keys only in target: 9001..9010                                            -> VC-01, VC-04
INSERT INTO tgt.Invoice (InvoiceID, CustomerNumber, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt)
SELECT InvoiceID + 9000, CustomerNumber, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt
FROM tgt.Invoice WHERE InvoiceID BETWEEN 1 AND 10;
-- T3  Duplicate keys in TARGET: 300 and 301 loaded twice (identical copies)                -> VC-01, VC-03
INSERT INTO tgt.Invoice SELECT * FROM tgt.Invoice WHERE InvoiceID IN (300, 301);
-- T4  Two rows with a NULL key in TARGET (copies of 50 with InvoiceID NULL)                -> VC-01, VC-02, VC-03
INSERT INTO tgt.Invoice (InvoiceID, CustomerNumber, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt)
SELECT NULL, CustomerNumber, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt
FROM tgt.Invoice WHERE InvoiceID = 50
UNION ALL
SELECT NULL, CustomerNumber, InvoiceDate, DueDate, Amount, Currency, Status, Notes, ExchangeRate, UpdatedAt
FROM tgt.Invoice WHERE InvoiceID = 50;
-- T5  Amount off by 0.01 on 1001..1020 (20 keys)                                          -> VC-05
UPDATE tgt.Invoice SET Amount = Amount + 0.01 WHERE InvoiceID BETWEEN 1001 AND 1020;
-- T6  InvoiceDate one day later on 1101..1105 (5 keys)                                    -> VC-05
UPDATE tgt.Invoice SET InvoiceDate = DATEADD(DAY, 1, InvoiceDate) WHERE InvoiceID BETWEEN 1101 AND 1105;
-- T7  Leading space in Status on 1401 (1 key)                                             -> VC-05
UPDATE tgt.Invoice SET Status = N' ' + Status WHERE InvoiceID = 1401;
-- T8  Empty string instead of NULL in Notes on 1510,1520,1530,1540,1550 (src Notes is NULL) -> VC-05
UPDATE tgt.Invoice SET Notes = N'' WHERE InvoiceID IN (1510, 1520, 1530, 1540, 1550);
-- T9  ExchangeRate float differs by 1e-15 on 1701 (1 key)                                 -> VC-05
UPDATE tgt.Invoice SET ExchangeRate = ExchangeRate + 1.0E-15 WHERE InvoiceID = 1701;
-- T10 DueDate NULL in source but filled in target on 1800 (1 key)                         -> VC-05
UPDATE tgt.Invoice SET DueDate = '2025-12-31' WHERE InvoiceID = 1800;
-- T11 Required column NULL in TARGET: Currency on 1901 (1 key)                            -> VC-02, VC-05
UPDATE tgt.Invoice SET Currency = NULL WHERE InvoiceID = 1901;

/* ================= Edge cases that must NOT be reported ================= */
-- N1  Trailing spaces in Status on 1201..1210: SQL Server ignores trailing spaces in comparisons
UPDATE tgt.Invoice SET Status = Status + N'   ' WHERE InvoiceID BETWEEN 1201 AND 1210;
-- N2  Upper-case Status on 1301..1310: the collation is case-insensitive (Latin1_General_CI_AI)
UPDATE tgt.Invoice SET Status = UPPER(Status) WHERE InvoiceID BETWEEN 1301 AND 1310;
-- N3  Accent removed in Notes on 1601..1603 ('Café' -> 'Cafe'): the collation is accent-insensitive
UPDATE tgt.Invoice SET Notes = REPLACE(Notes, N'Caf' + NCHAR(233), N'Cafe') WHERE InvoiceID BETWEEN 1601 AND 1603;
-- N4  UpdatedAt differs on every target row: compare: false in the mapping
UPDATE tgt.Invoice SET UpdatedAt = CAST('2026-01-15T08:30:00.123' AS DATETIME2(3));
GO

SET NOEXEC OFF;
GO
