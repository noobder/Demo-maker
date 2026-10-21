@echo off
cd /d %~dp0
call .venv\Scripts\activate.bat
echo Open http://127.0.0.1:8765 in your browser
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
