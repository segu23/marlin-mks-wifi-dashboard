@echo off
cd /d "%~dp0"
start "" http://localhost:8000
python server.py --printer 192.168.100.116 --port 8000
