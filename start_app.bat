@echo off
rem Starts the web app and keeps it running (restarts it if it stops).
rem Open http://127.0.0.1:8010 after a few seconds.
cd /d "%~dp0"
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0app_watchdog.ps1"
echo App starting... open http://127.0.0.1:8010
