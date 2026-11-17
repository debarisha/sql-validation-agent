/* 01_create_contract_test_data.sql  (SQL Server / T-SQL)
   Purpose : Builds a SANDBOX src.Contract and tgt.Contract with known, deliberate defects
             so every check in generated_sql/contract/ has something to find.
             Expected output of each check: test_data/expected_results.md
   SAFETY  : This script DROPS and recreates src.Contract and tgt.Contract.
             It refuses to run unless the current database is named ValidationSandbox.
             Create it first if needed:   CREATE DATABASE ValidationSandbox;
             Then:                        USE ValidationSandbox;  (and run this script)
   Tables have no primary key or NOT NULL constraints on purpose, so duplicates and NULLs
   can be planted. Column names match the placeholder mapping in mappings/contract.yaml. */

IF DB_NAME() <> N'ValidationSandbox'
BEGIN
    DECLARE @db NVARCHAR(128) = DB_NAME();
    RAISERROR(N'Wrong database. Run this only in ValidationSandbox (current: %s).', 16, 1, @db) WITH NOWAIT;
    SET NOEXEC ON;   -- stops the rest of the script from executing
END;
GO

IF SCHEMA_ID(N'src') IS NULL EXEC (N'CREATE SCHEMA src');
IF SCHEMA_ID(N'tgt') IS NULL EXEC (N'CREATE SCHEMA tgt');
GO

DROP TABLE IF EXISTS src.Contract;
DROP TABLE IF EXISTS tgt.Contract;
GO

CREATE TABLE src.Contract (
    ContractID INT            NULL,
    CustomerID INT            NULL,
    StartDate  DATE           NULL,
    EndDate    DATE           NULL,
    Amount     DECIMAL(18, 2) NULL,
    Status     NVARCHAR(20)   NULL
);

CREATE TABLE tgt.Contract (
    ContractID INT            NULL,
    CustomerID INT            NULL,
    StartDate  DATE           NULL,
    EndDate    DATE           NULL,
    Amount     DECIMAL(18, 2) NULL,
    Status     NVARCHAR(20)   NULL
);
GO

/* ---------------- Source: 9 clean rows ---------------- */
INSERT INTO src.Contract (ContractID, CustomerID, StartDate, EndDate, Amount, Status) VALUES
(1, 101, '2024-01-01', '2024-12-31', 1000.00, N'Active'),
(2, 102, '2024-02-01', NULL,         2500.50, N'Active'),   -- open-ended (NULL EndDate is allowed)
(3, 103, '2024-03-15', '2025-03-14',  750.00, N'Closed'),
(4, 104, '2024-04-01', '2025-03-31', 1200.00, N'Active'),
(5, 105, '2024-05-01', NULL,          300.00, NULL),        -- NULL Status is allowed
(6, 106, '2024-06-01', '2025-05-31',  999.99, N'Active'),
(7, 107, '2024-07-01', '2025-06-30',  100.00, N'Active'),
(8, 108, '2024-08-01', NULL,          450.00, N'Pending'),
(9, 109, '2024-09-01', '2025-08-31',  600.00, N'Active');

/* ---------------- Target: 10 rows with planted defects ---------------- */
INSERT INTO tgt.Contract (ContractID, CustomerID, StartDate, EndDate, Amount, Status) VALUES
(1,  101,  '2024-01-01', '2024-12-31', 1000.00, N'Active'),   -- exact match
(2,  102,  '2024-02-01', NULL,         2500.50, N'Active'),   -- match (NULL vs NULL is equal)
(3,  103,  '2024-03-15', '2025-03-14',  750.00, N'Closed'),   -- match ...
(3,  103,  '2024-03-15', '2025-03-14',  750.00, N'Closed'),   -- DEFECT: duplicate key 3          -> check 03
(4,  104,  '2024-04-01', '2025-03-31', 1200.00, N'Active'),   -- exact match
(5,  105,  '2024-05-01', NULL,          300.00, NULL),        -- match (NULLs on both sides)
(6,  NULL, '2024-06-01', '2025-05-31',  999.99, N'Active'),   -- DEFECT: required CustomerID NULL -> checks 02, 05
(7,  107,  '2024-07-01', '2025-06-30',  150.00, N'Active'),   -- DEFECT: Amount 100.00 -> 150.00  -> check 05
(8,  108,  '2024-08-01', '2025-07-31',  450.00, N'Closed'),   -- DEFECT: EndDate NULL -> date and
                                                              --         Status Pending -> Closed -> check 05
                                                              -- DEFECT: ContractID 9 not loaded -> checks 01, 04
(11, 111,  '2024-11-01', '2025-10-31',  820.00, N'Active');   -- DEFECT: extra key 11             -> checks 01, 04
GO

SET NOEXEC OFF;
GO
