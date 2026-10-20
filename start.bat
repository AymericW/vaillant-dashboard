@echo off
REM Double-click to start the dashboard. First run installs everything (needs Python 3.11+ from python.org).
REM Everything is inside one ( ... ) block so Windows reads the whole script before running it:
REM that way a "git pull" that updates this file cannot break the run in progress.
(
  cd /d "%~dp0"

  REM Update to the latest version from GitHub - needs Git for Windows, skipped if not installed
  if exist .git (
    where git >nul 2>nul && (
      echo Checking for updates...
      git pull --ff-only || echo Could not update - starting the current version.
    )
  )

  if not exist .venv (
    echo Creating Python environment...
    py -3 -m venv .venv || python -m venv .venv || (echo Python 3.11+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
  )
  REM install new requirements on every start, so updates pick up new packages
  .venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)

  if not exist config.toml (
    copy config.example.toml config.toml >nul
    echo.
    echo A config.toml was created. Fill in your login details - see README - then run start.bat again.
    echo Or run "start.bat --demo" to try it with fake data.
    notepad config.toml
    if "%~1"=="" exit /b 0
  )
  .venv\Scripts\python app.py %*
  pause
  exit /b
)
