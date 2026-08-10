@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PIDFILE=data\worker.pid"
if not exist "%PIDFILE%" goto not_found

set /p WPID=<"%PIDFILE%"
echo 正在停止后台任务执行进程（pid=%WPID%）...
taskkill /PID %WPID% /F >nul 2>nul
if errorlevel 1 goto kill_failed
echo [完成] 后台任务执行进程已停止。
del "%PIDFILE%" >nul 2>nul
goto end

:not_found
echo [提示] 未找到后台进程记录（data\worker.pid），可能未启动或已停止。
goto end

:kill_failed
echo [提示] 进程可能已停止，尝试清理 pid 记录。
del "%PIDFILE%" >nul 2>nul

:end
pause
