@echo off
chcp 65001 >nul
REM 双击这个文件启动服务，然后浏览器打开 http://127.0.0.1:8000
REM   /         业务界面（记账 / 审核 / 对账 / 报表 / 财务分析）
REM   /console  Agent 执行链路与审批面板
REM 关掉这个黑窗口就是停止服务。
REM
REM 默认用 PATH 里的 python；想指定某个虚拟环境的解释器，先设 PYTHON 环境变量：
REM   set PYTHON=D:\path\to\envs\your-env\python.exe

if "%PYTHON%"=="" set PYTHON=python

"%PYTHON%" -c "import fastapi" 2>nul
if errorlevel 1 (
    echo [错误] 当前 python 里没有装依赖。先执行：
    echo     pip install -r requirements.txt
    echo 或者设 PYTHON 指向已装好依赖的解释器。
    pause
    exit /b 1
)

"%PYTHON%" "%~dp0scripts\serve.py" 127.0.0.1 8000

echo.
echo 服务已停止。
pause
