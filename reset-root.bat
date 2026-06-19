@echo off
REM ============================================================
REM  Reset the MariaDB 'root' password to 'admin' (and repair the app 'admin'
REM  account) WITHOUT needing the current root password. Uses mysqld's
REM  --init-file, which runs with full privileges at startup.
REM
REM  RIGHT-CLICK this file -> "Run as administrator".
REM ============================================================
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo This script must be run as administrator.
    echo Right-click reset-root.bat and choose "Run as administrator".
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
echo [1/3] Stopping the MariaDB service...
net stop MariaDB

echo.
echo [2/3] Applying the password reset via --init-file...
start "" /b "%MDB%\mysqld.exe" --defaults-file="%MYINI%" --init-file="%~dp0reset-root.sql" --console
echo     waiting for the reset to apply...
timeout /t 8 /nobreak >nul
taskkill /IM mysqld.exe /F >nul 2>&1

echo.
echo [3/3] Restarting the MariaDB service...
net start MariaDB

echo.
echo ============================================================
echo  Done. Both accounts now use the password:  admin
echo      root  / admin
echo      admin / admin
echo  You can now run ZTPOS-Setup.exe and enter root / admin.
echo ============================================================
