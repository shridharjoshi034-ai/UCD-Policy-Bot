@echo off
setlocal

:: Run this file from the project root (the folder containing both
:: "backend" and "policybot-frontend" subfolders)

echo ============================================
echo  Select mode:
echo  1. Complete   - Ollama + Backend + Frontend
echo  2. Backend only - Ollama + Backend
echo ============================================
set /p MODE=Enter 1 or 2: 

if "%MODE%"=="1" goto complete
if "%MODE%"=="2" goto backend_only
echo Invalid input. Defaulting to Complete mode.
goto complete

:complete
echo Starting Ollama...
start "PolicyBot-Ollama" cmd /k "ollama serve"
timeout /t 10 >nul

echo Starting Backend...
start "PolicyBot-Backend" cmd /k "cd /d "%~dp0backend" && uvicorn app.main:app --reload --port 8000"
timeout /t 10 >nul

echo Starting Frontend...
start "PolicyBot-Frontend" cmd /k "cd /d "%~dp0policybot-frontend" && npm run dev"
goto running

:backend_only
echo Starting Ollama...
start "PolicyBot-Ollama" cmd /k "ollama serve"
timeout /t 10 >nul

echo Starting Backend...
start "PolicyBot-Backend" cmd /k "cd /d "%~dp0backend" && uvicorn app.main:app --reload --port 8000"
goto running

:running

echo.
echo ============================================
echo Services launched in separate windows.
echo Type "close" or "exit" below to shut everything down.
echo ============================================
echo.

:loop
set /p CMD=^>^>
if /i "%CMD%"=="close" goto shutdown
if /i "%CMD%"=="exit" goto shutdown
echo Type "close" or "exit" to shut down.
goto loop

:shutdown
echo.
echo Shutting down all services...

:: Kill whatever process is actually bound to each port (equivalent to Ctrl+C)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :5173 ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :11434 ^| findstr LISTENING') do taskkill /F /PID %%p >nul 2>&1

:: Close the terminal windows themselves
taskkill /FI "WINDOWTITLE eq PolicyBot-Ollama*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq PolicyBot-Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq PolicyBot-Frontend*" /T /F >nul 2>&1

:: Catch any stray ollama processes not caught above
taskkill /F /IM ollama.exe /T >nul 2>&1
taskkill /F /IM "ollama app.exe" /T >nul 2>&1

echo Done. All services and Ollama instances closed.
timeout /t 2 >nul
exit