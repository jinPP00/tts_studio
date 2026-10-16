@echo off
setlocal
set "TARGET_DIR=C:\projects\tts_studio"

if not exist "%TARGET_DIR%\launch.py" (
    if not exist "C:\projects" mkdir "C:\projects"
    git clone https://github.com/jinPP00/tts_studio.git "%TARGET_DIR%"
) else (
    pushd "%TARGET_DIR%"
    git pull -q >nul 2>&1
    popd
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%TARGET_DIR%\launch.py"
    goto :eof
)
where python >nul 2>&1
if %errorlevel%==0 (
    python "%TARGET_DIR%\launch.py"
    goto :eof
)
py -3 "%TARGET_DIR%\launch.py"