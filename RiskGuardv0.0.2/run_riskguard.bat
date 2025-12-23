@echo off
setlocal

set "APP_DIR=%~dp0"
set "VENV_PY=%APP_DIR%venv\Scripts\python.exe"
set "SETUP_PS=%APP_DIR%setup_riskguard.ps1"

if exist "%VENV_PY%" (
  "%VENV_PY%" -V >nul 2>&1
  if errorlevel 1 (
    echo Venv python is broken. Recreating...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%SETUP_PS%"
    if errorlevel 1 (
      echo Setup failed. Check logs\setup.log
      exit /b 1
    )
  )
)

if not exist "%VENV_PY%" (
  echo Venv python not found. Running setup...
  if not exist "%SETUP_PS%" (
    echo setup_riskguard.ps1 not found. Aborting.
    exit /b 1
  )
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SETUP_PS%"
  if errorlevel 1 (
    echo Setup failed. Check logs\setup.log
    exit /b 1
  )
)

if not exist "%VENV_PY%" (
  echo Venv python still not found. Aborting.
  exit /b 1
)

pushd "%APP_DIR%" >nul
"%VENV_PY%" "%APP_DIR%main.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

exit /b %EXIT_CODE%
