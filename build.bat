@echo off
rem Builds dist\ScreenRecorder (exe + bundled ffmpeg) and ScreenRecorder.zip for sharing.
cd /d "%~dp0"

set "FFMPEG=%~dp0ffmpeg.exe"
if not exist "%FFMPEG%" for %%F in (ffmpeg.exe) do set "FFMPEG=%%~$PATH:F"
if not exist "%FFMPEG%" (
    echo ERROR: ffmpeg.exe not found. Put ffmpeg.exe next to build.bat or add it to PATH.
    exit /b 1
)
echo Using ffmpeg: %FFMPEG%

python -m PyInstaller --noconfirm --clean --windowed --onedir --name ScreenRecorder ^
    --add-binary "%FFMPEG%;." recorder.pyw
if errorlevel 1 (
    echo ERROR: PyInstaller build failed, see the output above.
    exit /b 1
)

if exist ScreenRecorder.zip del ScreenRecorder.zip
powershell -NoProfile -Command "Compress-Archive -Path 'dist\ScreenRecorder' -DestinationPath 'ScreenRecorder.zip'"
if errorlevel 1 (
    echo ERROR: Could not create ScreenRecorder.zip.
    exit /b 1
)
echo Done: dist\ScreenRecorder\ScreenRecorder.exe and ScreenRecorder.zip
