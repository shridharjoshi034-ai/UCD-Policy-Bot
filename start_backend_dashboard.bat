@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  PolicyBot - Backend + Dashboard
echo ============================================
echo.

:: Kill any leftover process on port 8000
echo Cleaning up old backend instances...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do taskkill /F /PID %%p >nul 2>&1
timeout /t 1 >nul

:: Start the backend in its own window
echo Starting backend server (port 8000)...
cd /d "%~dp0backend"
start "PolicyBot-Backend" cmd /k "title PolicyBot-Backend && uvicorn app.main:app --host 127.0.0.1 --port 8000"
cd /d "%~dp0"

:: Wait for the backend to start up (embedding models take time)
echo Waiting 20 seconds for backend to start...
timeout /t 20

:: Open the dashboard
echo Opening dashboard...
start http://localhost:8000/admin/dashboard

echo.
echo ============================================
echo  Dashboard is open. Backend is running.
echo  If the page doesn't load, wait a moment
echo  and refresh - the server may still be
echo  loading embedding models.
echo.
echo  Type "close" below to stop everything.
echo ============================================
echo.

:loop
set /p CMD=^> 
if /i "!CMD!"=="close" goto shutdown
if /i "!CMD!"=="exit" goto shutdown
echo Type "close" to shut down.
goto loop

:shutdown
echo.
echo Shutting down all PolicyBot services...
taskkill /FI "WINDOWTITLE eq PolicyBot-Backend*" /T /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING" 2^>nul') do taskkill /F /PID %%p >nul 2>&1
echo All services stopped.
echo.
pause
exit
