@echo off
setlocal
pushd "%~dp0" || exit /b 1
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install uv, then follow docs/usage.md.
    pause
    popd
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo Install the application first: uv sync --locked --extra web
    pause
    popd
    exit /b 1
)
if not exist "frontend\dist\index.html" (
    echo Build the interface first: cd frontend ^&^& npm ci ^&^& npm run build
    pause
    popd
    exit /b 1
)
uv run --frozen --no-sync --extra web labweaver serve --open
set "LABWEAVER_LAUNCH_EXIT=%errorlevel%"
if not "%LABWEAVER_LAUNCH_EXIT%"=="0" pause
popd
exit /b %LABWEAVER_LAUNCH_EXIT%
