@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

rem ============================================================
rem  mmclaw dashboard one-shot stopper
rem  Usage:
rem    dashboard_stop.bat            stop local tunnel + http server
rem    dashboard_stop.bat --remote   ALSO stop remote services on the board
rem ============================================================

set "BOARD=root@192.168.31.51"
set "SSH_KEY=%USERPROFILE%\.ssh\id_rsa"
set "REMOTE_DIR=/root/mmclaw/services"
set "TUNNEL_PID_FILE=%TEMP%\mmclaw_tunnel.pid"
set "HTTP_PID_FILE=%TEMP%\mmclaw_http.pid"

set "STOP_REMOTE=0"
if /I "%~1"=="--remote" set "STOP_REMOTE=1"
if /I "%~1"=="-r"        set "STOP_REMOTE=1"

set "HAS_KEY=0"
if exist "%SSH_KEY%" set "HAS_KEY=1"

echo.
echo [mmclaw] dashboard stopper
set "STOPPED_ANY=0"

rem --- 1. stop SSH tunnel -----------------------------------------------
if exist "%TUNNEL_PID_FILE%" (
  set /p TUNNEL_PID=<"%TUNNEL_PID_FILE%"
  if defined TUNNEL_PID (
    echo [mmclaw] killing SSH tunnel PID !TUNNEL_PID! ...
    taskkill /F /T /PID !TUNNEL_PID! >nul 2>&1
    if errorlevel 1 (
      echo [mmclaw][WARN] PID !TUNNEL_PID! not running ^(stale pid file^).
    ) else (
      set "STOPPED_ANY=1"
    )
  )
  del /q "%TUNNEL_PID_FILE%" >nul 2>&1
) else (
  echo [mmclaw] no tunnel PID file - falling back to wmic command-line match
  call :kill_by_cmdline_match "ssh.exe" "18789:localhost:18790"
)

rem --- 2. stop python http.server ---------------------------------------
if exist "%HTTP_PID_FILE%" (
  set /p HTTP_PID=<"%HTTP_PID_FILE%"
  if defined HTTP_PID (
    echo [mmclaw] killing http.server PID !HTTP_PID! ...
    taskkill /F /T /PID !HTTP_PID! >nul 2>&1
    if errorlevel 1 (
      echo [mmclaw][WARN] PID !HTTP_PID! not running ^(stale pid file^).
    ) else (
      set "STOPPED_ANY=1"
    )
  )
  del /q "%HTTP_PID_FILE%" >nul 2>&1
) else (
  echo [mmclaw] no http PID file - falling back to wmic command-line match
  call :kill_by_cmdline_match "python.exe" "http.server"
)

rem --- 3. optionally stop remote ----------------------------------------
if "%STOP_REMOTE%"=="1" (
  echo [mmclaw] stopping remote services on %BOARD% ...
  if "%HAS_KEY%"=="1" (
    ssh -i "%SSH_KEY%" -o IdentitiesOnly=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new %BOARD% "cd %REMOTE_DIR% && ./stop_all.sh"
  ) else (
    ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new %BOARD% "cd %REMOTE_DIR% && ./stop_all.sh"
  )
  if errorlevel 1 (
    echo [mmclaw][WARN] remote stop_all.sh returned non-zero.
  ) else (
    echo [mmclaw] remote services stopped.
  )
) else (
  echo [mmclaw] remote services left running ^(pass --remote to also stop them^).
)

echo.
if "%STOPPED_ANY%"=="1" (
  echo [mmclaw] done. local processes terminated.
) else (
  echo [mmclaw] done. nothing local appeared to be running.
)
exit /b 0

rem --- helpers ---------------------------------------------------------
:kill_by_cmdline_match
rem %~1 = exe name (e.g. ssh.exe), %~2 = substring to match in CommandLine
set "EXE=%~1"
set "MATCH=%~2"
set "PS_FIND=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq '%EXE%' -and $_.CommandLine -like '*%MATCH%*' } | ForEach-Object { $_.ProcessId }"
for /f "tokens=*" %%P in ('powershell -NoProfile -Command "%PS_FIND%"') do (
  echo [mmclaw] killing %EXE% PID %%P ^(matched: %MATCH%^)
  taskkill /F /T /PID %%P >nul 2>&1
  if not errorlevel 1 set "STOPPED_ANY=1"
)
goto :eof
