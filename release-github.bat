@echo off
REM ===== Cut a release and publish it to GitHub Releases =====
REM
REM Bumps the version, rebuilds everything (incl. the update zip), and creates a
REM GitHub Release tagged v<version> with the update zip + installer attached.
REM The in-app updater reads GitHub's "latest release" automatically, so every
REM install offers the new version next time it checks.
REM
REM   release-github.bat            bump patch  (1.1.0 -> 1.1.1) then publish
REM   release-github.bat minor      bump minor  (1.1.0 -> 1.2.0) then publish
REM   release-github.bat major      bump major  then publish
REM   release-github.bat 1.4.2      set explicit version then publish
REM
REM Prerequisites (one-time):
REM   - GitHub CLI installed + authenticated:  gh auth login
REM   - This folder is a git repo with a GitHub remote (gh detects the repo)
REM   - Set GITHUB_REPO ("owner/name") in config.py AND installer_app\setup_wizard.py
REM   - Describe the changes under "## [Unreleased]" in CHANGELOG.md first
cd /d "%~dp0"

where gh >nul 2>nul || (
    echo ERROR: GitHub CLI 'gh' not found. Install from https://cli.github.com/
    echo Then run:  gh auth login
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

set "BUMP=%~1"
if "%BUMP%"=="" set "BUMP=patch"

echo Bumping version (%BUMP%)...
%PY% bump_version.py %BUMP% || exit /b 1

set /p APPVER=<VERSION

echo.
echo Building app payload + update package for v%APPVER% ...
call build-setup.bat || exit /b 1

if not exist "release\ZTPOS-%APPVER%.zip" (
    echo ERROR: release\ZTPOS-%APPVER%.zip was not produced by the build.
    exit /b 1
)

echo.
echo Building the online installer ...
call build-online-setup.bat || exit /b 1

echo.
echo Writing release notes from CHANGELOG.md ...
%PY% release_notes.py %APPVER% > "release\notes-%APPVER%.md"

echo.
echo Publishing GitHub Release v%APPVER% ...
REM Attach the app zip (what the installer downloads on install AND update) and
REM the online installer (for brand-new installs). --notes-file gives the
REM release body shown in the app's "Update available" notice.
set "ASSETS=release\ZTPOS-%APPVER%.zip"
if exist "setup\ZTPOS-Online-Setup.exe" set "ASSETS=%ASSETS% setup\ZTPOS-Online-Setup.exe"

gh release create "v%APPVER%" %ASSETS% ^
    --title "ZT POS v%APPVER%" ^
    --notes-file "release\notes-%APPVER%.md" || exit /b 1

echo.
echo ============================================================
echo  Published v%APPVER% to GitHub Releases.
echo  Installed apps will offer it on their next update check.
echo ============================================================
