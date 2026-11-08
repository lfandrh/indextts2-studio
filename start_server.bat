@echo off
rem ============================================================
rem  IndexTTS2 - start the FastAPI server in ONE console window.
rem
rem  KEEP THIS FILE PURE ASCII + CRLF.
rem  cmd.exe decodes .bat bytes with the OEM code page, so a UTF-8
rem  file turns into "'xxx' is not recognized as an internal or
rem  external command" on Chinese (GBK) consoles. All Chinese text
rem  lives in diagnose.py instead.
rem
rem  The server runs in the FOREGROUND of this window. There is no
rem  second cmd.exe and no minimized server window. Closing this
rem  window tears down the console and every process attached to it,
rem  so the server stops with the window. If server.py crashes, cmd
rem  gets control back immediately and runs the diagnostics instead of
rem  polling a dead port for 120 seconds.
rem
rem  On any failure, diagnose.py prints a readable Chinese
rem  explanation, keeps the COMPLETE original log, and writes a
rem  self-contained report to logs\last_error.txt that can be handed
rem  to an AI. If no usable Python exists at all, we fall back to the
rem  plain ASCII messages below so the window is never silent.
rem
rem  A tiny watcher is started with START /B in the SAME console (no
rem  new window). It waits for the port, prints the URL, opens the
rem  browser, then exits. Set INDEXTTS_NO_BROWSER=1 to skip the browser.
rem
rem  Every external tool is called through %SYS32% on purpose: some
rem  machines have no C:\Windows\System32 in PATH, which used to
rem  break powershell / ping / uv and made the server fail silently.
rem
rem  Usage:  start_server.bat            (default port 7860)
rem          start_server.bat 7861
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

rem Internal watcher mode. Never used by hand.
if /i "%~1"=="--watch" goto :watch_mode

call :init_env
if errorlevel 1 goto :no_windows
call :find_diag_py

set "PORT=7860"
if not "%~1"=="" set "PORT=%~1"
call :validate_port %PORT%
if errorlevel 1 goto :bad_port

set "HOST=127.0.0.1"
set "LOG=%~dp0logs\server_bg.log"
rem Override the log path for tests or a second instance. A stale background
rem server from an older script can hold server_bg.log open forever.
if defined INDEXTTS_LOG set "LOG=%INDEXTTS_LOG%"
set "READY_FLAG=%LOG%.ready"
set "EXIT_FLAG=%LOG%.exited"
set "HF_ENDPOINT=https://hf-mirror.com"
if not exist "%~dp0logs" md "%~dp0logs" >nul 2>&1

rem ============================================================
rem  IndexTTS2 inference acceleration config
rem  Edit the values below, save, then rerun this script.
rem  1 = on, 0 = off
rem
rem FP16=1          GPT main model in half precision (half VRAM, faster)
rem S2MEL_FP16=1    s2mel diffusion in FP16 (measured ~32% faster on long text; timbre shifts slightly)
rem W2V_FP16=1      w2v-bert semantic encoder in FP16. Saves ~1GB VRAM on 8G cards
rem                 (the single biggest VRAM saver). Test audio quality after disabling.
rem QWEN_FP16=1     Qwen emotion model in FP16 (it already loads as float16; this is
rem                 the status quo). Set 0 only to fall back to FP32 for debugging.
rem CUDNN_BENCHMARK=0 bigvgan/wavenet conv auto-tune. MEASURED SLOWDOWN on this model:
rem                   cuDNN re-tunes for every new input length, so BigVGAN goes
rem                   3.35s -> 97s and a whole run 38s -> 141s (RTF 6.1 -> 20.8).
rem                   Keep 0 unless your audio length never changes.
rem DIFFUSION_STEPS=25 CFM diffusion steps, 8..16 recommended (lower = faster)
rem CFG_RATE=0.7    CFG strength; 0 runs one pass per step, ~2x faster
rem ============================================================

set FP16=1
set S2MEL_FP16=1
set W2V_FP16=1
set QWEN_FP16=1
set CUDNN_BENCHMARK=0
set DIFFUSION_STEPS=25
set CFG_RATE=0.7

rem Build the server args from the config above.
rem Quoted SET is used everywhere so trailing spaces or & never leak in.
set "SRV_ARGS=--port %PORT%"
if "%FP16%"=="1" (set "SRV_ARGS=%SRV_ARGS% --fp16") else if "%FP16%"=="0" set "SRV_ARGS=%SRV_ARGS% --no-fp16"
if "%S2MEL_FP16%"=="1" (set "SRV_ARGS=%SRV_ARGS% --s2mel_fp16") else if "%S2MEL_FP16%"=="0" set "SRV_ARGS=%SRV_ARGS% --no-s2mel_fp16"
if "%W2V_FP16%"=="1" (set "SRV_ARGS=%SRV_ARGS% --w2v_fp16") else if "%W2V_FP16%"=="0" set "SRV_ARGS=%SRV_ARGS% --no-w2v_fp16"
if "%QWEN_FP16%"=="0" set "SRV_ARGS=%SRV_ARGS% --no-qwen_fp16"
if "%CUDNN_BENCHMARK%"=="1" set "SRV_ARGS=%SRV_ARGS% --cudnn_benchmark"
set "SRV_ARGS=%SRV_ARGS% --diffusion_steps %DIFFUSION_STEPS% --inference_cfg_rate %CFG_RATE%"

call :find_runner
if errorlevel 1 goto :no_runner

title IndexTTS2 server - port %PORT% - close this window to stop

echo ============================================
echo  IndexTTS2 FastAPI server - single window
echo ============================================
echo  UI:    http://%HOST%:%PORT%
echo  Log:   %LOG%
echo  Run:   %RUN_EXE%
echo  Error: %~dp0logs\last_error.txt  (give this file to an AI)
echo  Config:
echo    FP16=%FP16%  S2MEL_FP16=%S2MEL_FP16%  W2V_FP16=%W2V_FP16%  QWEN_FP16=%QWEN_FP16%  CUDNN_BENCHMARK=%CUDNN_BENCHMARK%
echo    DIFFUSION_STEPS=%DIFFUSION_STEPS%  CFG_RATE=%CFG_RATE%
echo --------------------------------------------
echo  The server runs in THIS window. Close the window to stop it.
echo ============================================
echo.

rem Fast, zero-cost source check. It only starts the diagnostics tool when
rem something is obviously missing, so a healthy start pays no extra process.
rem It runs BEFORE kill_port: never stop a working old server just to discover
rem that the source tree is gone.
call :check_source
if errorlevel 1 goto :preflight_failed

rem Stop an old instance on the target port so fresh code + config apply.
rem kill_port refuses to touch anything that is not our server.py.
echo Checking port %PORT% for an old instance...
call :kill_port %PORT%
if errorlevel 1 goto :port_busy

if exist "%READY_FLAG%" del /q "%READY_FLAG%" >nul 2>&1
if exist "%EXIT_FLAG%" del /q "%EXIT_FLAG%" >nul 2>&1
if exist "%LOG%" del /q "%LOG%" >nul 2>&1
if exist "%LOG%" goto :log_busy
rem If the log cannot be written, the redirect below fails and python exits
rem silently with code 0 - the exact confusing failure this check prevents.
echo. > "%LOG%" 2>nul
if errorlevel 1 goto :log_busy

rem START /B = same console, no second window. The watcher is short lived.
start "" /b "%COMSPEC%" /c ""%~f0" --watch %PORT% %HOST%"

if /i "%RUN_KIND%"=="uv" goto :launch_uv
echo Starting server in the foreground...
echo.
"%RUN_EXE%" server.py %SRV_ARGS% >> "%LOG%" 2>&1
goto :server_exited
:launch_uv
echo Starting server in the foreground via uv...
echo.
"%RUN_EXE%" run --extra server --no-sync python server.py %SRV_ARGS% >> "%LOG%" 2>&1

:server_exited
set "RC=%ERRORLEVEL%"
echo. > "%EXIT_FLAG%" 2>nul
rem The ready flag tells a startup failure apart from a normal stop. Without
rem it, closing the window on a healthy server would print a scary error.
set "WAS_READY=0"
if exist "%READY_FLAG%" set "WAS_READY=1"
if exist "%READY_FLAG%" del /q "%READY_FLAG%" >nul 2>&1
echo.
echo Server process exited with code %RC%. The server is stopped.
if not "%WAS_READY%"=="1" goto :server_exited_diag
echo The server had started successfully; this looks like a normal stop.
echo Runtime log: %LOG%
goto :server_done

:server_exited_diag
echo.
call :diag --analyze --rc %RC% --port "%PORT%" --host "%HOST%" --runner "%RUN_EXE%" --run-kind "%RUN_KIND%" --log "%LOG%"
if errorlevel 1 call :fallback_error
goto :server_done

:server_done
echo.
echo Press any key to close this window.
pause >nul
exit /b %RC%

rem ============================================================
rem  Failure branches. Each one asks diagnose.py for a readable message
rem  first; if there is no usable Python at all, keep the old ASCII text.
rem ============================================================

:preflight_failed
echo.
echo The server was NOT started. See the message above.
echo.
pause
exit /b 1

:port_busy
call :diag --code port_busy --port "%PORT%" --host "%HOST%" --log "%LOG%" --runner "%RUN_EXE%" --run-kind "%RUN_KIND%"
if not errorlevel 1 goto :branch_end
echo.
echo [ERROR] Port %PORT% is held by a process that is NOT this project's
echo         server.py. Nothing was killed. Free the port by hand or
echo         start on another port, for example: start_server.bat 7861
goto :branch_end

:log_busy
call :diag --code log_busy --port "%PORT%" --host "%HOST%" --log "%LOG%" --runner "%RUN_EXE%" --run-kind "%RUN_KIND%"
if not errorlevel 1 goto :branch_end
echo.
echo [ERROR] Cannot write the log file:
echo         %LOG%
echo         An old background server is probably still holding it open.
echo         Run stop_server.bat for that instance, or set INDEXTTS_LOG
echo         to a different path, then try again.
goto :branch_end

:bad_port
call :diag --code bad_port --port "%PORT%" --host "%HOST%" --log "%LOG%"
if not errorlevel 1 goto :branch_end
echo.
echo [ERROR] Invalid port: %PORT%
echo         Use a number between 1 and 65535.
echo         Usage: start_server.bat [port]
goto :branch_end

:no_runner
call :diag --code no_runner --port "%PORT%" --host "%HOST%" --log "%LOG%"
if not errorlevel 1 goto :branch_end
echo [ERROR] Neither uv nor a Python environment was found.
echo.
echo   - Install uv from https://docs.astral.sh/uv/ then run:
echo         uv sync --extra server
echo   - or put a runtime\python.exe or .venv\Scripts\python.exe
echo     next to this script.
goto :branch_end

:no_windows
call :diag --code no_windows
if not errorlevel 1 goto :branch_end
echo [ERROR] Cannot find the Windows system folder (System32).
echo         The Windows installation looks broken or is on an
echo         unexpected drive. Cannot start the server.
goto :branch_end

:branch_end
echo.
pause
exit /b 1

:fallback_error
rem Diagnostics unavailable: preserve the old behaviour, never a silent window.
echo.
if not "%RC%"=="0" (
  echo [ERROR] The server exited unexpectedly. Last lines of the log:
  echo ------------------------------------------------------------
  call :tail_log
  echo ------------------------------------------------------------
)
if not defined DIAG_PY (
  echo [ERROR] Chinese diagnostics unavailable - no usable Python was found.
) else (
  echo [ERROR] Chinese diagnostics unavailable - diagnose.py is missing or failed to run.
  echo          Check that diagnose.py sits next to start_server.bat.
)
exit /b 0

rem ============================================================
rem  Watcher mode: runs inside the same console, no new window.
rem  %2 = port, %3 = host
rem ============================================================
:watch_mode
call :init_env
if errorlevel 1 exit /b 1
call :find_diag_py
set "WPORT=%~2"
set "WHOST=%~3"
set "WLOG=%~dp0logs\server_bg.log"
if defined INDEXTTS_LOG set "WLOG=%INDEXTTS_LOG%"
set "WEXIT=%WLOG%.exited"
rem The parent writes .exited as soon as the server process is gone, so the
rem watcher stops right away instead of spinning for another 60 seconds.
rem Warn once after ~60s, then keep polling quietly so the ready flag stays
rem accurate even if the first model download takes several minutes.
for /l %%i in (1,1,60) do (
  if exist "%WEXIT%" exit /b 1
  call :port_open %WPORT%
  if not errorlevel 1 goto :watch_ready
  if exist "%SYS32%\ping.exe" (%SYS32%\ping.exe -n 2 127.0.0.1 >nul 2>&1) else (%SYS32%\timeout.exe /t 1 /nobreak >nul 2>&1)
)
call :diag --code watcher_timeout --port "%WPORT%" --host "%WHOST%" --log "%WLOG%"
for /l %%i in (1,1,120) do (
  if exist "%WEXIT%" exit /b 1
  call :port_open %WPORT%
  if not errorlevel 1 goto :watch_ready_quiet
  if exist "%SYS32%\ping.exe" (%SYS32%\ping.exe -n 2 127.0.0.1 >nul 2>&1) else (%SYS32%\timeout.exe /t 1 /nobreak >nul 2>&1)
)
exit /b 1

:watch_ready_quiet
echo. > "%WLOG%.ready" 2>nul
exit /b 0

:watch_ready
echo. > "%WLOG%.ready" 2>nul
echo.
echo Server is ready: http://%WHOST%:%WPORT%
echo Close this window to stop the server.
echo.
if /i "%INDEXTTS_NO_BROWSER%"=="1" exit /b 0
start "" "http://%WHOST%:%WPORT%"
exit /b 0

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
rem Find ANY usable Python for the diagnostics tool. This is deliberately
rem wider than :find_runner: on a machine whose venv/uv is missing we still
rem want a readable error, and that is exactly when it matters most.
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
rem %* = full argument list for diagnose.py.
rem Returns 0 when the tool ran, 1 when it is unavailable (ASCII fallback).
if not defined DIAG_PY exit /b 1
if not exist "%~dp0diagnose.py" exit /b 1
"%DIAG_PY%" "%~dp0diagnose.py" %*
exit /b %ERRORLEVEL%

:check_source
rem Pure cmd existence checks: zero extra processes on a healthy start.
set "PF_CODE="
if not exist "%~dp0server.py" set "PF_CODE=preflight_server"
if not defined PF_CODE if not exist "%~dp0indextts\__init__.py" set "PF_CODE=preflight_source"
if not defined PF_CODE if not exist "%~dp0indextts\utils\__init__.py" set "PF_CODE=preflight_source"
if not defined PF_CODE if not exist "%~dp0web\index.html" set "PF_CODE=preflight_source"
if not defined PF_CODE exit /b 0
call :diag --code %PF_CODE% --port "%PORT%" --host "%HOST%" --log "%LOG%" --runner "%RUN_EXE%" --run-kind "%RUN_KIND%"
if errorlevel 1 (
  echo [ERROR] Project files are missing:
  if not exist "%~dp0server.py" echo   server.py
  if not exist "%~dp0indextts\__init__.py" echo   indextts\
  if not exist "%~dp0indextts\utils\__init__.py" echo   indextts\utils\
  if not exist "%~dp0web\index.html" echo   web\
  echo Restore with: git restore -- indextts web examples
)
exit /b 1

:find_runner
rem Prefers a bundled runtime, then the local .venv, then uv.
set "RUN_EXE="
set "RUN_KIND="
if exist "%~dp0runtime\python.exe" set "RUN_EXE=%~dp0runtime\python.exe"
if defined RUN_EXE goto :runner_is_python
if exist "%~dp0.venv\Scripts\python.exe" goto :runner_venv

where uv >nul 2>&1 && set "RUN_EXE=uv"
if not defined RUN_EXE call :probe_file "%USERPROFILE%\.local\bin\uv.exe"
if not defined RUN_EXE call :probe_file "%LOCALAPPDATA%\Programs\uv\uv.exe"
if not defined RUN_EXE call :probe_file "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if not defined RUN_EXE call :probe_file "%USERPROFILE%\.cargo\bin\uv.exe"
if not defined RUN_EXE call :probe_file "%ProgramFiles%\uv\uv.exe"
if not defined RUN_EXE exit /b 1
set "RUN_KIND=uv"
exit /b 0

:runner_venv
set "RUN_EXE=%~dp0.venv\Scripts\python.exe"
:runner_is_python
set "RUN_KIND=python"
exit /b 0

:probe_file
if exist %1 set "RUN_EXE=%~1"
exit /b 0

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
rem Exit 0 = safe (nothing to stop, or stopped), 1 = refused to kill.
rem Never kill a random app just because it owns the port: verify the
rem command line first, and give up if it cannot be read.
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

:tail_log
if not exist "%LOG%" (
  echo [no log file: %LOG%]
  exit /b 0
)
if not defined PWSH (
  echo [open %LOG% to see the error]
  exit /b 0
)
"%PWSH%" -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 25 -ErrorAction SilentlyContinue"
exit /b 0
