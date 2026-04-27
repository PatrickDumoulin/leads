@echo off
cd /d "e:\Compagnies\Dumoulin Solutions\Sonoria\Developpement\Combine Leadlists"

for /f %%i in (server.pid) do set OLD_PID=%%i

start "" C:\Python314\python.exe -m waitress --host=127.0.0.1 --port=5000 --threads=8 app:app

timeout /t 4 /nobreak > nul

taskkill /f /pid %OLD_PID% > nul 2>&1
