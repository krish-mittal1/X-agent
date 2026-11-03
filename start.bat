@echo off
title X Autonomous Agent
echo ====================================================
echo        X Autonomous Agent (100%% ₹0 Automated)
echo ====================================================
echo.

:: 1. Ensure Postgres database container is running
echo [1/3] Ensuring Database (PostgreSQL) is running...
docker start techx-pg >nul 2>&1

:: 2. Open dashboard in browser after a short delay
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8000/dashboard"

:: 3. Start Agent Server & Scheduler
echo [2/3] Starting Autonomous Agent Engine...
echo [3/3] Opening Dashboard: http://localhost:8000/dashboard
echo.
echo Agent is now running autonomously!
echo - Pulls live breaking news every 20 min
echo - Drafts viral takes via Groq every 45 min
echo - Posts to X automatically via Brave every ~48 min
echo.
echo Press Ctrl+C in this terminal window anytime to stop.
echo ====================================================
echo.

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause

