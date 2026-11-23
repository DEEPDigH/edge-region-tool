@echo off
chcp 65001 >nul
python -m pip install --target "%~dp0lib" -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo 依赖安装失败，请检查 Python 与网络后重试。
) else (
  echo 依赖安装完成，可以打开工具。
)
pause
