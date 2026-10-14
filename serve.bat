@echo off
chcp 65001 >nul
REM 双击这个文件启动服务，然后浏览器打开 http://127.0.0.1:8000
REM   /         业务界面（记账 / 审核 / 对账 / 报表 / 财务分析）
REM   /console  Agent 执行链路与审批面板
REM 关掉这个黑窗口就是停止服务。

set PYTHON=D:\Users\Lenovo\anaconda3\envs\langchain1.2\python.exe

if not exist "%PYTHON%" (
    echo [错误] 找不到 Python：%PYTHON%
    echo 改一下这个文件里的 PYTHON 变量。
    pause
    exit /b 1
)

"%PYTHON%" "%~dp0scripts\serve.py" 127.0.0.1 8000

echo.
echo 服务已停止。
pause
