@echo off
rem 带控制台的启动方式：排查问题时用，出错信息会留在窗口里
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "APPDIR=%~dp0"
cd /d "%APPDIR%"
set "PY="

call :probe py -3
if not defined PY call :probe python
if not defined PY (
  for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  ) do (
    if not defined PY if exist %%P call :probe %%P
  )
)
if not defined PY (
  echo 没有找到可用的 Python 3，请先安装 Python。
  pause
  exit /b 1
)
echo 使用 Python：%PY%
%PY% "%APPDIR%gui.py"
echo.
echo 程序已退出，按任意键关闭窗口。
pause >nul
exit /b 0

:probe
"%~1" %2 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=%~1 %2"
exit /b 0
