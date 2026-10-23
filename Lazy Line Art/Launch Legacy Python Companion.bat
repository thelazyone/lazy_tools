@echo off
cd /d "%~dp0"
python path_companion\app.py
if errorlevel 1 pause
