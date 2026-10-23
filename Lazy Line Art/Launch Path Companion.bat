@echo off
cd /d "%~dp0"
if exist "updates\Path Companion.exe" (
    copy /y "updates\Path Companion.exe" "Path Companion.exe" >nul 2>nul
    if errorlevel 1 (
        "updates\Path Companion.exe" %*
        exit /b
    )
    del /q "updates\Path Companion.exe" >nul 2>nul
)
if not exist "Path Companion.exe" (
    echo Building Path Companion...
    cargo build --release --locked --manifest-path path_companion_rust\Cargo.toml
    if errorlevel 1 (
        pause
        exit /b 1
    )
    copy /y "path_companion_rust\target\release\lazy-path-companion.exe" "Path Companion.exe" >nul
)
"Path Companion.exe" %*
if errorlevel 1 pause
