@echo off
setlocal
chcp 65001 >nul 2>&1
title Live Caption One-Click Installer

set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%windows\install-oneclick.ps1"

if not exist "%PS1%" (
  echo Missing installer: "%PS1%"
  pause
  exit /b 1
)

net session >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator privileges...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo Install failed with exit code %RC%.
) else (
  echo Install complete.
)
pause
exit /b %RC%
