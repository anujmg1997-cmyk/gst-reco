@echo off
REM Local Windows build (same steps as the GitHub release build).
REM Produces dist\installer\GSTReco-Setup-<version>.exe
REM Needs Python 3.10+ and Inno Setup 6 (https://jrsoftware.org/isinfo.php).
cd /d "%~dp0"
set VER=%1
if "%VER%"=="" set VER=1.0.0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --clean --windowed --name GSTReco --icon assets\app.ico ^
  --add-data "gstreco\portal_selectors.json;gstreco" --add-data "assets\app.png;assets" ^
  --collect-all playwright app.py || goto :err
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
%ISCC% /DMyAppVersion=%VER% installer\GSTReco.iss || goto :err
echo.
echo Installer: dist\installer\GSTReco-Setup-%VER%.exe
pause
goto :eof
:err
echo Build failed - see messages above.
pause
