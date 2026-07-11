@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_queries.ps1"
echo.
echo ============================================
echo Script finished (or errored above). Press any key to close.
echo ============================================
pause >nul
