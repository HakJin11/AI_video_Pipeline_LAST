@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8010 --host 127.0.0.1 >> uvicorn_stdout.log 2>> uvicorn_stderr.log
