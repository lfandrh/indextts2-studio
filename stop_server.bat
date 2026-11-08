@echo off
rem ============================================================
rem  IndexTTS2 - stop the server that listens on a given port.
rem
rem  KEEP THIS FILE PURE ASCII + CRLF (see start_server.bat).
rem  All Chinese messages live in diagnose.py.
rem
rem  Safety: a port number alone does not prove ownership. Before any
rem  process is stopped we read its command line and require server.py.
rem  Anything else is reported and left alone, so this script can never
rem  kill an unrelated app that happens to hold the same port.
rem
rem  Usage:  stop_server.bat          (default port 7860)
rem          stop_server.bat 7861
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

call :init_env
if errorlevel 1 goto :no_windows
call :find_diag_py

set "PORT=7860"
if not "%~1"=="" set "PORT=%~1"
call :validate_port %PORT%
if errorlevel 1 goto :bad_port

set "RC=0"
title Stop IndexTTS2 server - port %PORT%
echo Stopping IndexTTS2 server on port %PORT% ...
call :port_open %PORT%
if errorlevel 1 goto :nothing

call :kill_port %PORT%
if errorlevel 1 goto :refused

call :wait_free %PORT% 10
if errorlevel 1 goto :still_busy
call :diag --code stop_ok --port "%PORT%"
if errorlevel 1 echo Server stopped.
goto :end

:refused
set "RC=1"
call :diag --code stop_refused --port "%PORT%"
if not errorlevel 1 goto :end
echo.
echo [ERROR] Nothing was stopped. The process on port %PORT% does not
echo         look like this project's server.py. Refusing to kill it.
goto :end

:still_busy
set "RC=1"
call :diag --code stop_still_busy --port "%PORT%"
if not errorlevel 1 goto :end
echo.
echo [WARN] Port %PORT% is still listening after the stop request.
echo        Check the owning process by hand before restarting.
goto :end

:nothing
call :diag --code stop_nothing --port "%PORT%"
if not errorlevel 1 goto :end
echo No server is listening on port %PORT% - already stopped.
goto :end

:bad_port
set "RC=1"
call :diag --code stop_bad_port --port "%PORT%"
if not errorlevel 1 goto :end
echo.
echo [ERROR] Invalid port: %PORT%
echo         Use a number between 1 and 65535.
echo         Usage: stop_server.bat [port]
goto :end

:no_windows
set "RC=1"
call :diag --code stop_no_windows
if not errorlevel 1 goto :end
echo [ERROR] Cannot find the Windows system folder (System32).
echo         Check the Windows installation; cannot stop the server.
goto :end

:end
echo.
pause
exit /b %RC%

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

:find_diag_py
rem Find ANY usable Python for the diagnostics tool (see start_server.bat).
set "DIAG_PY="
if exist "%~dp0runtime\python.exe" set "DIAG_PY=%~dp0runtime\python.exe"
if defined DIAG_PY exit /b 0
if exist "%~dp0.venv\Scripts\python.exe" set "DIAG_PY=%~dp0.venv\Scripts\python.exe"
if defined DIAG_PY exit /b 0
if exist "%SystemRoot%\py.exe" set "DIAG_PY=%SystemRoot%\py.exe"
if defined DIAG_PY exit /b 0
for /f "delims=" %%P in ('%SYS32%\where.exe python.exe 2^>nul') do if not defined DIAG_PY set "DIAG_PY=%%P"
if defined DIAG_PY exit /b 0
exit /b 1

:diag
rem %* = full argument list for diagnose.py. Returns 1 if unavailable.
if not defined DIAG_PY exit /b 1
if not exist "%~dp0diagnose.py" exit /b 1
"%DIAG_PY%" "%~dp0diagnose.py" %*
exit /b %ERRORLEVEL%

:validate_port
rem %1 = candidate port. Pure digits, 1..65535, nothing injectable.
set "VP=%~1"
if not defined VP exit /b 1
for /f "delims=0123456789" %%a in ("%VP%") do exit /b 1
if %VP% LSS 1 exit /b 1
if %VP% GTR 65535 exit /b 1
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
rem %1 = port. Stops ONLY listeners whose command line names server.py.
rem Exit 0 = safe, 1 = refused to kill.
if not defined PWSH goto :kill_port_netstat
"%PWSH%" -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; $c = @(Get-NetTCPConnection -LocalPort %1 -State Listen); if ($c.Count -eq 0) { Write-Host '  nothing listening.'; exit 0 }; $bad = 0; $ids = @($c | Select-Object -ExpandProperty OwningProcess -Unique); foreach ($id in $ids) { $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $id); $cl = if ($p) { $p.CommandLine } else { '' }; if (-not $cl -or $cl -notmatch 'server\.py') { Write-Host ('  REFUSED pid ' + $id + ' - not an IndexTTS2 server.py process'); if ($cl) { Write-Host ('    command line: ' + $cl) }; $bad = 1 } else { Write-Host ('  stopping pid ' + $id + ' (server.py)'); Stop-Process -Id $id -Force } }; if ($bad -eq 1) { exit 1 }; exit 0"
exit /b %ERRORLEVEL%

:kill_port_netstat
rem No PowerShell available: only python.exe / uv.exe listeners are safe
rem enough to stop, and we still refuse everything else.
set "KP_REFUSED="
for /f "tokens=5" %%p in ('%SYS32%\netstat.exe -ano -p tcp ^| %SYS32%\findstr.exe /r /c:":%1[ ]*.*LISTENING"') do call :kill_one_netstat %%p
if defined KP_REFUSED exit /b 1
echo   nothing listening.
exit /b 0

:kill_one_netstat
%SYS32%\tasklist.exe /FI "PID eq %1" /NH 2>nul | %SYS32%\findstr.exe /i /c:"python.exe" /c:"uv.exe" >nul 2>&1
if errorlevel 1 (
  echo   REFUSED pid %1 - not python.exe or uv.exe
  set "KP_REFUSED=1"
  exit /b 0
)
echo   stopping pid %1
%SYS32%\taskkill.exe /f /pid %1 >nul 2>&1
exit /b 0

:wait_free
rem %1 = port, %2 = attempts. Exit 0 once free, 1 if still busy.
rem Keep the PowerShell call OUTSIDE a parenthesised block: inside one,
rem %ERRORLEVEL% would be expanded before the call ever ran.
if not defined PWSH goto :wait_free_loop
"%PWSH%" -NoProfile -Command "for ($i = 0; $i -lt %2; $i++) { if (-not (Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue)) { exit 0 }; Start-Sleep -Milliseconds 500 }; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%
:wait_free_loop
for /l %%i in (1,1,%2) do (
  call :port_open %1
  if errorlevel 1 exit /b 0
  if exist "%SYS32%\ping.exe" (%SYS32%\ping.exe -n 2 127.0.0.1 >nul 2>&1) else (%SYS32%\timeout.exe /t 1 /nobreak >nul 2>&1)
)
exit /b 1
