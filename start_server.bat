@echo off
chcp 65001 >nul
REM process_6 - Visual Flow + Figure Planner (Gemini) server launcher
REM Step 1: install backend deps (first run)  Step 2: encrypt key.env  Step 3: run server
setlocal
cd /d "%~dp0backend"

echo [1/3] Installing backend dependencies (first run only)...
pip install -e .

echo [2/3] Encrypting key.env with Windows DPAPI (creates ..\key.env.enc)...
python -m figure_planner.encrypt_key_env

echo [3/3] Starting server at http://localhost:8000
start "" cmd /c "timeout /t 3 >nul && explorer http://localhost:8000"
python -m figure_planner.run_server

endlocal
pause
