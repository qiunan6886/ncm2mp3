@echo off
rem ============================================================
rem  ncm2mp3 启动器：自动寻找可用的 Python 3，然后打开图形界面
rem ============================================================
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
    "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  ) do (
    if not defined PY if exist %%P call :probe %%P
  )
)

if not defined PY (
  echo.
  echo   没有找到可用的 Python 3。
  echo.
  echo   请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/windows/
  echo   安装时记得勾选 "Add python.exe to PATH"。
  echo   装好后再双击本文件即可。
  echo.
  pause
  exit /b 1
)

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   找到了 Python（%PY%），但它缺少 tkinter 组件，无法显示界面。
  echo   请重新安装 Python 并勾选 "tcl/tk and IDLE"。
  echo.
  pause
  exit /b 1
)

echo 使用 Python：%PY%
if /i "%PY%"=="py -3" (
  start "" pyw -3 "%APPDIR%gui.py"
  exit /b 0
)
set "PYW=!PY:python.exe=pythonw.exe!"
if exist "!PYW!" (
  start "" "!PYW!" "%APPDIR%gui.py"
) else (
  echo 未找到 pythonw，改用控制台方式启动。
  "!PY%" "%APPDIR%gui.py"
  if errorlevel 1 pause
)
exit /b 0

:probe
rem 真正执行一次，排除 Microsoft Store 的占位程序
"%~1" %2 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=%~1 %2"
exit /b 0
