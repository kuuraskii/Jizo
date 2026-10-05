@echo off
REM One-command setup for Team Praann - Windows
REM Run: setup_venv.bat
python --version
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Setup done. Run: .venv\Scripts\activate.bat
