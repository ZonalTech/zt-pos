@echo off
REM ============================================================
REM  Creates the 'admin' MariaDB account used by the app (.env),
REM  then creates the pos_db database, tables and default admin login.
REM
REM  RIGHT-CLICK this file and choose "Run as administrator".
REM ============================================================
cd /d "%~dp0"

REM --- must be elevated to stop/start the Windows service ---
net session >nul 2>&1
if errorlevel 1 (
    echo This script must be run as administrator.
    echo Right-click fix-database.bat and choose "Run as administrator".
    pause
    exit /b 1
)

set "MDB=C:\Program Files\MariaDB 11.4\bin"
set "MYINI=C:\Program Files\MariaDB 11.4\data\my.ini"
if not exist "%MDB%\mysqld.exe" (
    echo Could not find MariaDB at "%MDB%".
    echo Edit the MDB path at the top of this script to match your install.
    pause
    exit /b 1
)

echo.
echo [1/4] Stopping MariaDB service...
net stop MariaDB

echo.
echo [2/4] Creating the 'admin' database account...
start "" /b "%MDB%\mysqld.exe" --defaults-file="%MYINI%" --init-file="%~dp0db_setup.sql" --console
echo     waiting for the account setup to apply...
timeout /t 8 /nobreak >nul
taskkill /IM mysqld.exe /F >nul 2>&1

echo.
echo [3/4] Restarting MariaDB service...
net start MariaDB

echo.
echo [4/4] Creating the database, tables and default admin...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" init_db.py || goto :error
    ".venv\Scripts\python.exe" migrate.py
) else (
    python init_db.py || goto :error
    python migrate.py
)

echo.
echo ============================================================
echo  Done. Start the app with run.bat and sign in:
echo      username: admin
echo      password: admin
echo  (you'll be prompted to set a new password right away)
echo ============================================================
pause
goto :eof

:error
echo.
echo Database setup failed - see the messages above.
pause
