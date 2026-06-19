@echo off
REM ===== POS launcher (Windows) =====
cd /d "%~dp0"

if not exist ".venv\" (
    echo Creating virtual environment...
    python -m venv .venv
    call ".venv\Scripts\activate.bat"
    echo Installing dependencies...
    pip install -r requirements.txt
) else (
    call ".venv\Scripts\activate.bat"
)

if not exist ".env" (
    echo No .env found - copying from .env.example. Edit it if your MariaDB password differs.
    copy ".env.example" ".env" >nul
)

echo Initializing database (safe to run every time)...
python init_db.py || goto :error

echo.
echo Starting POS (opens in its own app window)...
python launcher.py
goto :eof

:error
echo.
echo Startup failed. See the messages above (usually MariaDB not running or wrong password in .env).
pause
