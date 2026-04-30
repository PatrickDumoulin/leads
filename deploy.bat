@echo off
cd /d "e:\Compagnies\Dumoulin Solutions\Sonoria\Developpement\Combine Leadlists"

set LOGFILE=deploy_log.txt
echo [%date% %time%] === DEPLOY START === >> %LOGFILE%

echo [%date% %time%] Git pull... >> %LOGFILE%
"C:\Program Files\Git\cmd\git.exe" pull >> %LOGFILE% 2>&1
echo [%date% %time%] Git pull exit code: %errorlevel% >> %LOGFILE%

echo [%date% %time%] Killing old python.exe... >> %LOGFILE%
taskkill /f /im python.exe >> %LOGFILE% 2>&1
timeout /t 2 /nobreak > nul

echo [%date% %time%] Starting new server... >> %LOGFILE%
start "" C:\Python314\python.exe -m waitress --host=127.0.0.1 --port=5000 --threads=8 app:app

timeout /t 4 /nobreak > nul

for /f "tokens=5" %%i in ('netstat -ano ^| findstr "127.0.0.1:5000" ^| findstr "LISTENING"') do (
    echo %%i> server.pid
    echo [%date% %time%] New server PID: %%i >> %LOGFILE%
    goto :done
)
echo [%date% %time%] WARNING: Could not find server PID on port 5000 >> %LOGFILE%

:done
echo [%date% %time%] === DEPLOY END === >> %LOGFILE%
echo. >> %LOGFILE%
echo Deploye OK
