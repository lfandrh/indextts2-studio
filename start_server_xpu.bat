@echo off
rem ============================================================
rem  IndexTTS2 - start the server on the Intel iGPU (XPU).
rem
rem  Thin wrapper around start_server.bat: it only presets
rem  DEVICE=xpu, everything else (port, fp16, logs) is identical.
rem  Keep this file pure ASCII + CRLF, same reason as the main bat.
rem
rem  Note: on this machine (Ultra 5 245K) XPU measured SLOWER than
rem  CPU (39.6s vs 30.9s for a 2.7s clip). Worth re-testing after
rem  the BIOS update / memory OC: faster DDR5 directly feeds the iGPU.
rem
rem  Usage:  start_server_xpu.bat            (default port 7860)
rem          start_server_xpu.bat 7861
rem ============================================================
set DEVICE=xpu
rem Immediate command lists: measured ~5-8%% faster GPT decode on this iGPU.
set SYCL_PI_LEVEL_ZERO_USE_IMMEDIATE_COMMANDLISTS=1
call "%~dp0start_server.bat" %*
exit /b %ERRORLEVEL%
