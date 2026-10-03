@echo off
setlocal
rem CV Tailor for Windows: double-click to start.
rem The first run sets everything up in .venv (a minute or two); after that it just starts the app and
rem opens it in your browser. To use another port:  set CV_TAILOR_PORT=5001  before running it.

cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
set "MARK=.venv\.installed"
if not defined CV_TAILOR_PORT set "CV_TAILOR_PORT=5000"
set "URL=http://127.0.0.1:%CV_TAILOR_PORT%/"

rem Already running? Just open it.
if exist "%PY%" (
  "%PY%" -c "import sys, urllib.request as u; sys.exit(b'CV Tailor' not in u.urlopen('%URL%', timeout=2).read())" >nul 2>&1 && (
    echo CV Tailor is already running at %URL%
    start "" "%URL%"
    exit /b 0
  )
)

rem First run: create .venv. (Kept outside a parenthesised block so BASE_PY is read after it is set.)
if exist "%PY%" goto :install
echo Setting up CV Tailor for the first time. This takes a minute or two.
call :find_python || goto :fail
echo Creating .venv with %BASE_PY% ...
%BASE_PY% -m venv .venv || goto :fail
"%PY%" -m pip install --quiet --upgrade pip

:install

rem Install, or update after the project's dependencies changed (e.g. after a git pull).
"%PY%" -c "import os, sys; sys.exit(not (os.path.exists(r'%MARK%') and os.path.getmtime(r'%MARK%') >= os.path.getmtime('pyproject.toml')))" >nul 2>&1
if errorlevel 1 (
  echo Installing CV Tailor and its dependencies ...
  "%PY%" -m pip install --quiet -e . || goto :fail
  type nul > "%MARK%"
  call :offer_apply
  echo.
  echo Using OpenAI, Azure, Bedrock or Groq? Install its SDK once, e.g.:  .venv\Scripts\pip install -e ".[openai]"
  echo OpenRouter and Ollama need nothing extra.
  echo.
)

echo Starting CV Tailor. Your browser opens in a moment; close this window to stop it.
"%PY%" -m cv_maker --port %CV_TAILOR_PORT% %*
if errorlevel 1 goto :fail
exit /b 0

:find_python
rem Python 3.11 or newer: the "py" launcher first, then "python" on PATH.
set "BASE_PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE_PY=py -3"
if not defined BASE_PY python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1 && set "BASE_PY=python"
if defined BASE_PY exit /b 0
echo Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH"), then double-click this file again.
exit /b 1

:offer_apply
"%PY%" -c "import playwright" >nul 2>&1 && exit /b 0
echo.
echo Optional: the apply assistant fills in job applications in a private browser while you watch.
choice /c YN /n /m "Install it now (about 150 MB)? [Y/N] "
if errorlevel 2 (
  echo Skipped. Run this later to add it:  .venv\Scripts\pip install -e ".[apply]"  then  .venv\Scripts\python -m playwright install chromium
  exit /b 0
)
"%PY%" -m pip install --quiet -e ".[apply]" && "%PY%" -m playwright install chromium
exit /b 0

:fail
echo.
echo Something went wrong (see the messages above). Nothing was changed outside this folder.
pause
exit /b 1
