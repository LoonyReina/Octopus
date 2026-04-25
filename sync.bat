@echo off
REM mmclaw incremental sync wrapper.
REM Runs sync.py which streams changed files via ssh+tar to the dev board.
setlocal
set SCRIPT_DIR=%~dp0
python "%SCRIPT_DIR%sync.py" %*
exit /b %ERRORLEVEL%
