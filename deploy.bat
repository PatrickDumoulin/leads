@echo off
cd /d "e:\Compagnies\Dumoulin Solutions\Sonoria\Developpement\Combine Leadlists"

for /f %%i in (server.pid) do set OLD_PID=%%i

"C:\Program Files\Git\cmd\git.exe" pull

start "" C:\Python314\python.exe -m waitress --host=127.0.0.1 --port=5000 --threads=8 app:app

timeout /t 4 /nobreak > nul

for /f "tokens=5" %%i in ('netstat -ano ^| findstr "127.0.0.1:5000" ^| findstr "LISTENING"') do set NEW_PID=%%i
echo %NEW_PID%> server.pid

taskkill /f /pid %OLD_PID% > nul 2>&1
echo Deploye - nouveau PID: %NEW_PID%
