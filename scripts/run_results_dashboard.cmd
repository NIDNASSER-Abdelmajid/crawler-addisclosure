@echo off
setlocal

if not exist "dashboard-vite" (
  echo dashboard-vite folder was not found.
  exit /b 1
)

cmd /c "cd dashboard-vite && npm run desktop"
set "EXIT_CODE=%ERRORLEVEL%"

exit /b %EXIT_CODE%
