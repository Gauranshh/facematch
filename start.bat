@echo off
REM Windows: double-click. First run sets everything up, then starts the website.
cd /d "%~dp0"
if not exist .venv ( py -3 -m venv .venv )
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt && python download_models.py && python run.py
pause
