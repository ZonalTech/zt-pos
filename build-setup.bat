@echo off
REM ===== Build the app payload + update package =====
REM Compiles POS.exe, then packages the release artifacts the ONLINE installer
REM (and the in-app updater) download from GitHub:
REM     release\ZTPOS-<version>.zip     POS.exe
REM     release\manifest.json           {version, url, notes, sha256}
REM
REM No Uninstall.exe is built: the installer itself uninstalls (--uninstall),
REM so the payload is just POS.exe — a much smaller download.
REM
REM This does NOT build an installer. The single shippable installer is the
REM online bootstrapper - build it with build-online-setup.bat (it downloads
REM the zip above at install/update time). Run this first, then that.
cd /d "%~dp0"

if not exist ".venv\" (
    echo Creating build virtual environment...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo Installing build dependencies...
REM "python -m pip" (not bare pip): the pip.exe shim embeds an absolute python
REM path that breaks if the venv is moved; -m pip always resolves to the active
REM interpreter. Same reasoning applies to "python -m PyInstaller" below.
python -m pip install -q -r build-requirements.txt || goto :error

REM ===== Optional code signing =====
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
    echo Code signing disabled.
)

REM Intermediates live under setup\build\ and are removed at the end. This does
REM NOT wipe setup\ itself, so build-online-setup.bat can drop its installer
REM there afterwards.
set "WORK=setup\build"
set "PYI=setup\build\_pyi"
set "STAGE=setup\build\app"

echo Cleaning previous build output...
if exist "%WORK%\" rmdir /s /q "%WORK%"
if exist "build\" rmdir /s /q "build"
if exist "dist\" rmdir /s /q "dist"

echo [1/1] Compiling the POS app (POS.exe, single file)...
python -m PyInstaller pos.spec --noconfirm --distpath "%WORK%" --workpath "%PYI%" || goto :error

echo Staging the executable into a flat payload (%STAGE%)...
mkdir "%STAGE%"
copy /y "%WORK%\POS.exe" "%STAGE%\POS.exe" >nul || goto :error

if defined DO_SIGN (
    echo Signing the executable...
    call :sign "%STAGE%\POS.exe" || goto :error
)

REM ===== Update package =====
REM Produces release\ZTPOS-<version>.zip + release\manifest.json from the
REM (signed) exes. These are published as GitHub release assets; the online
REM installer downloads the zip on install AND on update.
if not defined UPDATE_BASE_URL set "UPDATE_BASE_URL="
echo Packaging the app payload (zip + manifest) into release\ ...
python make_update.py --source "%STAGE%" --base-url "%UPDATE_BASE_URL%" || goto :error

echo Cleaning up intermediates...
if exist "%WORK%\" rmdir /s /q "%WORK%"

echo.
echo ============================================================
echo  Done.  Release assets:
echo     release\ZTPOS-^<version^>.zip   +   release\manifest.json
echo  Now build the installer:  build-online-setup.bat
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
