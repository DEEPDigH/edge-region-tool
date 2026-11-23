@echo off
chcp 65001 >nul
set PYTHONUTF8=1
python "%~dp0edge_region.py" set US
echo.
pause
