@echo off
chcp 936 >nul
cd /d "%~dp0"
title 文档导演台

rem ============================================================
rem  找一个真能用的 python。
rem  坑：PATH 里的 python 常见是微软商店的「应用执行别名」，
rem      它只是几十字节的转发壳，对应的商店包缺失/损坏时，
rem      一运行就弹「应用程序无法正常启动 0xc0000142」。
rem  所以这里优先用真实安装路径，并且每个候选都实际跑一下验证。
rem ============================================================
set "PY="
call :pick "%LOCALAPPDATA%\Python\bin\python.exe"
call :pick "C://comfyui//ComfyUI//python_embeded//python.exe"
call :pick "C://comfyui//python_embeded//python.exe"
call :pick "%~dp0..\python_embeded\python.exe"
call :pick "%~dp0..\..\python_embeded\python.exe"
call :pick "D://comfyui//python_embeded//python.exe"
if not defined PY call :pickpy
if not defined PY set "PY=python"

echo   解释器：%PY%
echo.
echo  启动中，浏览器会自动打开 http://127.0.0.1:8771
echo  （这个黑窗口别关，关掉服务就停了）
echo.

"%PY%" doc_director.py
set "RC=%ERRORLEVEL%"

if "%RC%"=="2" goto dup
if "%RC%"=="0" goto normal
goto failed

rem ---------------- 子过程：挑一个能用的解释器 ----------------
:pick
if defined PY goto :eof
if not exist "%~1" goto :eof
if "%~z1"=="0" goto :eof
"%~1" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof

:pickpy
set "PY=py"
"py" -c "import sys" >nul 2>nul
if errorlevel 1 set "PY="
goto :eof

:dup
echo.
echo  已经有一个导演台在跑了，页面应该已经打开。
echo  如果浏览器没反应，手动打开 http://127.0.0.1:8771
echo  （要重启的话，先把那个旧的黑窗口关掉）
echo.
ping -n 10 127.0.0.1 >nul
exit /b 0

:normal
echo.
echo  程序已退出。
pause
exit /b 0

:failed
echo.
echo  [X] 程序异常退出（代码 %RC%），上面是原因。
echo      完整信息也写在 启动错误.txt 里。
echo.
echo      如果看到的是「应用程序无法正常启动 0xc0000142」，那是 python 本身坏了：
echo        打开本文件，在 set "PY=" 下面加一行：
echo            set "PY=C://你的//python.exe"
pause
exit /b 1

:nopython
echo.
echo  [X] 没找到能用的 python
echo.
echo      1) 装一个 Python 3.9 以上，安装时勾选 Add Python to PATH
echo      2) 或者把本目录放到 ComfyUI 的 python_embeded 同级
echo      3) 或者右键本文件 - 编辑，在 set "PY=" 下面加一行：
echo            set "PY=C://你的//python.exe"
echo.
pause
exit /b 1
