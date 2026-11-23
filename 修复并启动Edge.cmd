@echo off
chcp 65001 >nul
set PYTHONUTF8=1
python "%~dp0edge_region.py" launch US --repair-writing
if errorlevel 1 pause
