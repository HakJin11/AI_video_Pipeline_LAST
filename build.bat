@echo off
rem Build release\AIVideoServer.exe and release\AIVideoClient.exe
rem First time: python -m venv .buildenv ^&^& .buildenv\Scripts\python -m pip install pyinstaller
cd /d "%~dp0"
set PY=.buildenv\Scripts\python.exe
if not exist %PY% (
  echo .buildenv not found. Run: python -m venv .buildenv and .buildenv\Scripts\python -m pip install pyinstaller
  exit /b 1
)
%PY% -m PyInstaller --noconfirm --onefile --console --name AIVideoServer --paths common --distpath release --workpath build --specpath build server\udp_server.py || exit /b 1
%PY% -m PyInstaller --noconfirm --onefile --windowed --name AIVideoClient --paths common --distpath release --workpath build --specpath build client\udp_client.py || exit /b 1
echo.
echo Done: release\AIVideoServer.exe, release\AIVideoClient.exe
