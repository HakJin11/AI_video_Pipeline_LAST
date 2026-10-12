@echo off
rem Start the UDP server (settings in server_config.json next to this file: port, password).
cd /d "%~dp0"
if not exist server_config.json copy server_config.example.json server_config.json >nul
if exist AIVideoServer.exe (AIVideoServer.exe) else (python udp_server.py)
pause
