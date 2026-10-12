@echo off
rem Run the client from source (no build needed). Needs Python 3.11+ on PATH.
cd /d "%~dp0"
python udp_client.py
