@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "APP_URL=http://localhost:8000/pages/literature_library/index.html"
set "HEALTH_URL=http://localhost:8000/api/health"

echo [1/5] Checking Docker Desktop...
docker info >nul 2>nul
if errorlevel 1 (
  echo.
  echo Docker Desktop is not running, or Docker cannot be reached.
  echo Please start Docker Desktop first, wait until it is fully running, then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo [2/5] Checking local configuration...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure-local-env.ps1" -EnvPath "%~dp0.env" -TemplatePath "%~dp0.env.example"
if errorlevel 1 (
  echo.
  echo Failed to initialize .env. Review the message above, then try again.
  echo.
  pause
  exit /b 1
)

echo [3/5] Starting Literature AI services...
docker compose up -d
if errorlevel 1 (
  echo.
  echo Failed to start services. Recent backend logs:
  docker compose logs --tail=80 backend
  echo.
  pause
  exit /b 1
)

echo [4/5] Waiting for backend health check...
set "READY=0"
for /l %%i in (1,1,90) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH_URL%' -TimeoutSec 2; if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>nul
  if not errorlevel 1 (
    set "READY=1"
    goto :open_page
  )
  timeout /t 2 /nobreak >nul
)

:open_page
if not "%READY%"=="1" (
  echo.
  echo Backend did not become ready on %HEALTH_URL%.
  echo Current service status:
  docker compose ps
  echo.
  echo Recent backend logs:
  docker compose logs --tail=120 backend
  echo.
  echo Keep this window open and send the log above if it still fails.
  pause
  exit /b 1
)

echo [5/5] Opening Literature AI...
start "" "%APP_URL%"

echo.
echo Literature AI is ready:
echo %APP_URL%
echo.
timeout /t 5 /nobreak >nul
