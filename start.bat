@echo off
cd /d "%~dp0"
echo Installing deps if needed...
py -m pip install -r requirements.txt -q
echo.
echo Starting Covered Call desk...
echo Keep this window open. Close = stop.
echo Then open: http://127.0.0.1:5174/
echo.
py server.py
pause
