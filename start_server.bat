@echo off
rem ============================================================
rem  IndexTTS2 - start the FastAPI server in the background.
rem
rem  KEEP THIS FILE PURE ASCII + CRLF.
rem  cmd.exe decodes .bat bytes with the OEM code page, so a UTF-8
rem  file turns into "'xxx' is not recognized as an internal or
rem  external command" on Chinese (GBK) consoles.
rem
rem  Every external tool is called through %SYS32% on purpose: some
rem  machines have no C:\Windows\System32 in PATH, which used to
rem  break powershell / ping / uv and made the server fail to start
rem  with no useful message.
rem ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

call :init_env
if errorlevel 1 goto :no_windows

set "PORT=7860"
if not "%~1"=="" set "PORT=%~1"
set "HOST=127.0.0.1"
set "LOG=%~dp0logs\server_bg.log"
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

rem Build the server args from the config above
set "SRV_ARGS=--port %PORT%"
if "%FP16%"=="1" (set SRV_ARGS=%SRV_ARGS% --fp16) else if "%FP16%"=="0" set SRV_ARGS=%SRV_ARGS% --no-fp16
if "%S2MEL_FP16%"=="1" (set SRV_ARGS=%SRV_ARGS% --s2mel_fp16) else if "%S2MEL_FP16%"=="0" set SRV_ARGS=%SRV_ARGS% --no-s2mel_fp16
if "%W2V_FP16%"=="1" (set SRV_ARGS=%SRV_ARGS% --w2v_fp16) else if "%W2V_FP16%"=="0" set SRV_ARGS=%SRV_ARGS% --no-w2v_fp16
if "%QWEN_FP16%"=="0" set SRV_ARGS=%SRV_ARGS% --no-qwen_fp16
if "%CUDNN_BENCHMARK%"=="1" set SRV_ARGS=%SRV_ARGS% --cudnn_benchmark
set "SRV_ARGS=%SRV_ARGS% --diffusion_steps %DIFFUSION_STEPS% --inference_cfg_rate %CFG_RATE%"

call :find_runner
if errorlevel 1 goto :no_runner

echo ============================================
echo  IndexTTS2 FastAPI server (background)
echo  Log:  %LOG%
echo  UI:   http://%HOST%:%PORT%
echo  Run:  %RUN_EXE%
echo  Config:
echo    FP16=%FP16%  S2MEL_FP16=%S2MEL_FP16%  W2V_FP16=%W2V_FP16%  QWEN_FP16=%QWEN_FP16%  CUDNN_BENCHMARK=%CUDNN_BENCHMARK%
echo    DIFFUSION_STEPS=%DIFFUSION_STEPS%  CFG_RATE=%CFG_RATE%
echo ============================================
echo.

rem Stop any old server instance on the target port so the fresh code + config take effect
echo Checking port %PORT% for an old instance...
call :kill_port %PORT%

if exist "%LOG%" del /q "%LOG%" >nul 2>&1

if /i "%RUN_KIND%"=="uv" goto :launch_uv
start "IndexTTS2-server" /min "%COMSPEC%" /c ""%RUN_EXE%" server.py %SRV_ARGS% >> "%LOG%" 2>&1"
goto :launched
:launch_uv
start "IndexTTS2-server" /min "%COMSPEC%" /c ""%RUN_EXE%" run --extra server --no-sync python server.py %SRV_ARGS% >> "%LOG%" 2>&1"
:launched

echo Waiting for the server to listen on port %PORT% (up to 120s)...
call :wait_port %PORT% 120
if errorlevel 1 goto :not_ready

echo Server is ready: http://%HOST%:%PORT%
start "" "http://%HOST%:%PORT%"
goto :end

:not_ready
echo.
echo [ERROR] The server did not listen on port %PORT% within 120s.
echo         Last lines of the log:
echo ------------------------------------------------------------
call :tail_log
echo ------------------------------------------------------------
echo Fix the error above, then run this script again.
goto :end

:end
echo.
echo Server runs in the background. You can close this window;
echo the server keeps running. Rerun this script to restart with
echo the latest code/config. Use stop_server.bat to stop it.
echo.
pause
exit /b 0

:no_windows
echo [ERROR] Cannot find the Windows system folder (System32).
echo         The Windows installation looks broken or is on an
echo         unexpected drive. Cannot start the server.
echo.
pause
exit /b 1

:no_runner
echo [ERROR] Neither uv nor a Python environment was found.
echo.
echo   - Install uv from https://docs.astral.sh/uv/ then run:
echo         uv sync --extra server
echo   - or put a runtime\python.exe or .venv\Scripts\python.exe
echo     next to this script.
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

:find_runner
rem Prefers a bundled runtime, then uv, then the local .venv.
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

:wait_port
rem %1 = port, %2 = timeout in seconds. Exit 0 when ready, 1 on timeout.
if not defined PWSH goto :wait_port_loop
"%PWSH%" -NoProfile -Command "for ($i = 0; $i -lt %2; $i++) { if (Get-NetTCPConnection -LocalPort %1 -State Listen -ErrorAction SilentlyContinue) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
exit /b %ERRORLEVEL%
:wait_port_loop
for /l %%i in (1,1,%2) do (
  call :port_open %1
  if not errorlevel 1 exit /b 0
  if exist "%SYS32%\ping.exe" (%SYS32%\ping.exe -n 2 127.0.0.1 >nul 2>&1) else (%SYS32%\timeout.exe /t 1 /nobreak >nul 2>&1)
)
exit /b 1

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
