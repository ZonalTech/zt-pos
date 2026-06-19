@echo off
REM ===== Build the ONLINE installer: setup\ZTPOS-Online-Setup.exe =====
REM A tiny bootstrapper that downloads the latest release from GitHub at
REM install time, so it does NOT need POS.exe / Uninstall.exe to be compiled
REM first. Ship this one small file to install on any 64-bit Windows PC
REM straight from GitHub. It also serves as the updater (run with --update).
REM
REM It writes into setup\ WITHOUT wiping it, so it can run after build-setup.bat
REM (the offline installer) and both exes end up side by side in setup\.
cd /d "%~dp0"

if not exist ".venv\" (
    echo Creating build virtual environment...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo Installing build dependencies...
REM Use "python -m pip" (not bare "pip"): the pip.exe shim embeds an absolute
REM python path that breaks if the venv/python is ever moved, while -m pip
REM always resolves against the active interpreter.
python -m pip install -q -r build-requirements.txt || goto :error

REM ===== Optional code signing (same scheme as build-setup.bat) =====
REM Set SIGN_THUMBPRINT=<sha1>  OR  SIGN_PFX=<path> [SIGN_PFX_PASS=<pwd>] to sign
REM (so the UAC / SmartScreen popup reads "Zonal Tech" instead of "Unknown").
set "DO_SIGN="
if defined SIGN_THUMBPRINT set "DO_SIGN=1"
if defined SIGN_PFX set "DO_SIGN=1"
if defined DO_SIGN (
    where signtool >nul 2>nul || (
        echo ERROR: signing requested but signtool.exe is not on PATH.
        echo Install the Windows SDK or open a "Developer Command Prompt".
        goto :error
    )
    echo Code signing ENABLED ^(publisher will read "Zonal Tech"^).
) else (
    echo Code signing disabled - installer popup will show "Unknown publisher".
)

set "OUT=setup"
set "PYI=setup\build\_pyi_online"

echo Building the online installer (no payload bundled - it downloads from GitHub)...
python -m PyInstaller setup_online.spec --noconfirm --distpath "%OUT%" --workpath "%PYI%" || goto :error

if defined DO_SIGN (
    echo Signing the installer...
    call :sign "%OUT%\ZTPOS-Online-Setup.exe" || goto :error
)

echo Cleaning up intermediates...
if exist "%PYI%\" rmdir /s /q "%PYI%"

echo.
echo ============================================================
echo  Done.  Ship this single file:
echo     setup\ZTPOS-Online-Setup.exe
echo  Run it on any 64-bit Windows PC - it asks for the admin
echo  password, downloads the LATEST POS release from GitHub
echo  (ZonalTechnologies/zt-pos), installs MariaDB + the app, and
echo  starts. No app files are baked into the installer, so the
echo  same exe always installs whatever the newest release is.
echo ============================================================
goto :eof

:error
echo.
echo Build failed. See messages above.
exit /b 1

REM ===== signtool wrapper: signs %1 with a timestamp, SHA-256 =====
:sign
set "TS=http://timestamp.digicert.com"
if defined SIGN_TIMESTAMP_URL set "TS=%SIGN_TIMESTAMP_URL%"
if defined SIGN_THUMBPRINT (
    signtool sign /sha1 %SIGN_THUMBPRINT% /fd SHA256 /tr "%TS%" /td SHA256 %1 || exit /b 1
) else (
    if defined SIGN_PFX_PASS (
        signtool sign /f "%SIGN_PFX%" /p "%SIGN_PFX_PASS%" /fd SHA256 /tr "%TS%" /td SHA256 %1 || exit /b 1
    ) else (
        signtool sign /f "%SIGN_PFX%" /fd SHA256 /tr "%TS%" /td SHA256 %1 || exit /b 1
    )
)
exit /b 0
