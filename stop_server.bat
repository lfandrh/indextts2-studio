@echo off
rem ============================================================
rem  IndexTTS2 - stop the server that listens on a given port.
rem
rem  KEEP THIS FILE PURE ASCII + CRLF (see start_server.bat).
rem  Usage:  stop_server.bat          (default port 7860)
rem          stop_server.bat 7861
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

call :init_env
if errorlevel 1 goto :no_windows

set "PORT=7860"
if not "%~1"=="" set "PORT=%~1"

title Stop IndexTTS2 server
echo Stopping IndexTTS2 server on port %PORT% ...
call :port_open %PORT%
if errorlevel 1 goto :nothing

call :kill_port %PORT%
echo Server stopped.
goto :end

:nothing
echo No server is listening on port %PORT% (already stopped?).

:end
echo.
pause
exit /b 0

:no_windows
echo [ERROR] Cannot find the Windows system folder (System32).
echo         Check the Windows installation; cannot stop the server.
echo.
pause
exit /b 1

rem ============================================================
rem  Subroutines
rem ============================================================

:init_env
rem Rebuild the parts of the environment that a broken PATH loses.
set "SYS32="
if not defined SystemRoot if exist "C:\Windows\System32\cmd.exe" set "SystemRoot=C:\Windows"
if defined SystemRoot if exist "%SystemRoot%\System32\cmd.exe" set "SYS32=%SystemRoot%\System32"
if not defined SYS32 if exist "C:\Windows\System32\cmd.exe" set "SYS32=C:\Windows\System32"
if not defined SYS32 if exist "D:\Windows\System32\cmd.exe" set "SYS32=D:\Windows\System32"
if not defined SYS32 if exist "E:\Windows\System32\cmd.exe" set "SYS32=E:\Windows\System32"
if not defined SYS32 if exist "F:\Windows\System32\cmd.exe" set "SYS32=F:\Windows\System32"
if not defined SYS32 exit /b 1

for %%I in ("%SYS32%\..") do set "SystemRoot=%%~fI"
set "COMSPEC=%SYS32%\cmd.exe"
set "PWSH=%SYS32%\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PWSH%" set "PWSH="
if not defined PATHEXT set "PATHEXT=.COM;.EXE;.BAT;.CMD;.VBS;.JS;.WSF;.WSH;.MSC"
if defined PATH (set "PATH=%SYS32%;%SYS32%\Wbem;%SYS32%\WindowsPowerShell\v1.0;%PATH%") else set "PATH=%SYS32%;%SYS32%\Wbem;%SYS32%\WindowsPowerShell\v1.0"
exit /b 0

:port_open
rem %1 = port. Exit 0 when something is listening, 1 otherwise.
if not defined PWSH goto :port_open_netstat
"%PWSH%" -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
exit /b %ERRORLEVEL%
:port_open_netstat
%SYS32%\netstat.exe -ano -p tcp | %SYS32%\findstr.exe /r /c:":%1[ ]*.*LISTENING" >nul 2>&1
exit /b %ERRORLEVEL%

:kill_port
rem %1 = port. Force-stops every process listening on it.
if not defined PWSH goto :kill_port_netstat
"%PWSH%" -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue; if (-not $c) { Write-Host '  nothing listening.'; exit 0 }; $c | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { $p = Get-Process -Id $_ -ErrorAction SilentlyContinue; if ($p) { Write-Host ('  stopping pid ' + $_ + ' (' + $p.ProcessName + ')'); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } }"
exit /b 0
:kill_port_netstat
for /f "tokens=5" %%p in ('%SYS32%\netstat.exe -ano -p tcp ^| %SYS32%\findstr.exe /r /c:":%1[ ]*.*LISTENING"') do %SYS32%\taskkill.exe /f /pid %%p >nul 2>&1
exit /b 0
