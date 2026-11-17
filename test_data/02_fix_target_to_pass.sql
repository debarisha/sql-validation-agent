/* 02_fix_target_to_pass.sql  (SQL Server / T-SQL)
   Purpose : After you have seen the checks FAIL on the planted data, run this to make
             tgt.Contract an exact copy of src.Contract. Re-run the five checks; all should PASS.
   SAFETY  : Same guard as 01: only runs in the ValidationSandbox database. */

IF DB_NAME() <> N'ValidationSandbox'
BEGIN
    DECLARE @db NVARCHAR(128) = DB_NAME();
    RAISERROR(N'Wrong database. Run this only in ValidationSandbox (current: %s).', 16, 1, @db) WITH NOWAIT;
    SET NOEXEC ON;
END;
GO

TRUNCATE TABLE tgt.Contract;

INSERT INTO tgt.Contract (ContractID, CustomerID, StartDate, EndDate, Amount, Status)
SELECT ContractID, CustomerID, StartDate, EndDate, Amount, Status
FROM src.Contract;
GO

SET NOEXEC OFF;
GO
