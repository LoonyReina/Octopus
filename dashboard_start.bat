@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

rem ============================================================
rem  mmclaw dashboard one-shot launcher
rem  - Starts remote services on the board (idempotent)
rem  - Opens local SSH tunnel (background, PID tracked)
rem  - Serves dashboard/ via python http.server (background, PID tracked)
rem  - Opens default browser at http://localhost:8000
rem  Stop with: dashboard_stop.bat
rem ============================================================

set "BOARD=root@192.168.31.51"
set "SSH_KEY=%USERPROFILE%\.ssh\id_rsa"
set "REMOTE_DIR=/root/mmclaw/services"
set "TUNNEL_LOG=%TEMP%\mmclaw_tunnel.log"
set "HTTP_LOG=%TEMP%\mmclaw_http.log"
set "TUNNEL_PID_FILE=%TEMP%\mmclaw_tunnel.pid"
set "HTTP_PID_FILE=%TEMP%\mmclaw_http.pid"
set "SCRIPT_DIR=%~dp0"

echo.
echo [mmclaw] dashboard one-shot launcher
echo [mmclaw] script dir: %SCRIPT_DIR%
echo.

rem --- 0. sanity checks --------------------------------------------------
where ssh >nul 2>&1
if errorlevel 1 (
  echo [mmclaw][ERR] 'ssh' not found on PATH. Install OpenSSH ^(Windows
  echo               optional feature^) or use git-bash's ssh.
  goto :fail
)
where python >nul 2>&1
if errorlevel 1 (
  echo [mmclaw][ERR] 'python' not found on PATH. Install Python 3 first.
  goto :fail
)
where powershell >nul 2>&1
if errorlevel 1 (
  echo [mmclaw][ERR] 'powershell' not found on PATH. Required for PID capture.
  goto :fail
)
if not exist "%SCRIPT_DIR%dashboard\index.html" (
  echo [mmclaw][ERR] dashboard\index.html not found under %SCRIPT_DIR%
  goto :fail
)

set "HAS_KEY=1"
if not exist "%SSH_KEY%" (
  echo [mmclaw][WARN] ssh key not found at %SSH_KEY%
  echo                Falling back to default ssh-agent / config.
  set "HAS_KEY=0"
)

rem --- 1. start remote services (idempotent) -----------------------------
echo [mmclaw] (1/7) starting remote services on %BOARD% ...
if "%HAS_KEY%"=="1" (
  ssh -i "%SSH_KEY%" -o IdentitiesOnly=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new %BOARD% "cd %REMOTE_DIR% && ./run_all.sh"
) else (
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new %BOARD% "cd %REMOTE_DIR% && ./run_all.sh"
)
if errorlevel 1 (
  echo [mmclaw][ERR] remote run_all.sh failed. Check SSH connectivity:
  echo                ssh %BOARD%
  goto :fail
)

rem --- 2. wait ----------------------------------------------------------
echo [mmclaw] (2/7) waiting 2s for services to come up ...
timeout /t 2 /nobreak >nul

rem --- 3. start ssh tunnel in background --------------------------------
echo [mmclaw] (3/7) checking local ports 18789 / 8000 ...
call :check_port 18789 PORT_TUNNEL_BUSY
call :check_port 8000  PORT_HTTP_BUSY

if "%PORT_TUNNEL_BUSY%"=="1" (
  echo [mmclaw][WARN] port 18789 already in use - skipping tunnel start.
  echo                If the existing tunnel is stale, run dashboard_stop.bat first.
  goto :after_tunnel
)

echo [mmclaw] starting SSH tunnel ^(local 18789 -^> remote 18790, local 8080 -^> remote 8080^)
echo [mmclaw] tunnel log: %TUNNEL_LOG%

rem Build the ssh argument array in PowerShell cleanly to avoid quoting hell.
rem We use `Start-Process -PassThru` to capture the PID; $args.Id is printed.
set "PS_TUNNEL=$a = @(); if ('%HAS_KEY%' -eq '1') { $a += '-i'; $a += '%SSH_KEY%'; $a += '-o'; $a += 'IdentitiesOnly=yes' }; $a += '-o'; $a += 'ServerAliveInterval=30'; $a += '-o'; $a += 'ExitOnForwardFailure=yes'; $a += '-o'; $a += 'StrictHostKeyChecking=accept-new'; $a += '-N'; $a += '-L'; $a += '18789:localhost:18790'; $a += '-L'; $a += '8080:localhost:8080'; $a += '%BOARD%'; $p = Start-Process -FilePath 'ssh' -ArgumentList $a -WindowStyle Hidden -PassThru -RedirectStandardOutput '%TUNNEL_LOG%' -RedirectStandardError '%TUNNEL_LOG%.err'; $p.Id"

for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "%PS_TUNNEL%"`) do set "TUNNEL_PID=%%P"

if not defined TUNNEL_PID (
  echo [mmclaw][ERR] failed to spawn ssh tunnel - see %TUNNEL_LOG%.err
  goto :fail
)
> "%TUNNEL_PID_FILE%" echo !TUNNEL_PID!
echo [mmclaw] tunnel PID = !TUNNEL_PID! ^(saved to %TUNNEL_PID_FILE%^)

:after_tunnel

rem --- 4. wait for tunnel -----------------------------------------------
echo [mmclaw] (4/7) waiting 1s for tunnel ...
timeout /t 1 /nobreak >nul

rem --- 5. start local http server ---------------------------------------
if "%PORT_HTTP_BUSY%"=="1" (
  echo [mmclaw][WARN] port 8000 already in use - skipping http.server start.
  echo                Assuming an existing dashboard server. If wrong, run dashboard_stop.bat.
  goto :after_http
)

echo [mmclaw] (5/7) starting python http.server on :8000 serving dashboard/
echo [mmclaw] http log: %HTTP_LOG%

set "PS_HTTP=$a = @('-m','http.server','8000','--directory','dashboard'); $p = Start-Process -FilePath 'python' -ArgumentList $a -WorkingDirectory '%SCRIPT_DIR:~0,-1%' -WindowStyle Hidden -PassThru -RedirectStandardOutput '%HTTP_LOG%' -RedirectStandardError '%HTTP_LOG%.err'; $p.Id"

for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "%PS_HTTP%"`) do set "HTTP_PID=%%P"

if not defined HTTP_PID (
  echo [mmclaw][ERR] failed to spawn http.server - see %HTTP_LOG%.err
  goto :fail
)
> "%HTTP_PID_FILE%" echo !HTTP_PID!
echo [mmclaw] http PID = !HTTP_PID! ^(saved to %HTTP_PID_FILE%^)

:after_http

rem --- 6. wait + 7. open browser ---------------------------------------
echo [mmclaw] (6/7) waiting 1s for http server ...
timeout /t 1 /nobreak >nul

echo [mmclaw] (7/7) opening browser: http://localhost:8000
start "" "http://localhost:8000"

echo.
echo ============================================================
echo [mmclaw] dashboard is up.
echo   - URL:        http://localhost:8000
echo   - Tunnel PID: %TUNNEL_PID_FILE%
echo   - HTTP PID:   %HTTP_PID_FILE%
echo   - Logs:       %TUNNEL_LOG%
echo                 %HTTP_LOG%
echo.
echo   Stop local only:           dashboard_stop.bat
echo   Stop local + remote svcs:  dashboard_stop.bat --remote
echo ============================================================
exit /b 0

rem --- helpers ----------------------------------------------------------
:check_port
rem %1 = port number, %2 = output variable name (set to 0 or 1)
set "%2=0"
for /f "tokens=*" %%L in ('netstat -ano -p tcp ^| findstr ":%1 " ^| findstr LISTENING') do (
  set "%2=1"
)
goto :eof

:fail
echo.
echo [mmclaw][FATAL] startup aborted.
exit /b 1
