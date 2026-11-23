@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 创建虚拟环境并安装依赖 ===
if not exist "venv\Scripts\python.exe" (
  py -3.11 -m venv venv
  if errorlevel 1 (
    echo 创建失败：请确保已安装 Python 3.11 或以上版本。
    pause
    exit /b 1
  )
)
"venv\Scripts\python.exe" -m pip install -r requirements.txt
if not exist ".env" copy ".env.example" ".env" >nul
echo.
echo 完成。请编辑 .env 填入密钥；也可以直接双击“双击运行_演示报告.bat”查看效果。
pause
