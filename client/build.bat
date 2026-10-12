@echo off
rem Build AIVideoClient.exe next to this file. Needs Python 3.11+ on PATH (python).
cd /d "%~dp0"
if not exist .buildenv\Scripts\python.exe (
  python -m venv .buildenv || exit /b 1
  .buildenv\Scripts\python -m pip install pyinstaller || exit /b 1
)
.buildenv\Scripts\python -m PyInstaller --noconfirm --onefile --windowed --name AIVideoClient --distpath . --workpath build --specpath build udp_client.py || exit /b 1
echo Done: AIVideoClient.exe
