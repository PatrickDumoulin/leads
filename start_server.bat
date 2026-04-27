@echo off
cd /d "e:\Compagnies\Dumoulin Solutions\Sonoria\Developpement\Combine Leadlists"
C:\Python314\python.exe -m waitress --host=127.0.0.1 --port=5000 --threads=8 app:app
