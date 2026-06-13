@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "TAG="
if exist "%SCRIPT_DIR%\version.txt" (
    set /p TAG=<"%SCRIPT_DIR%\version.txt"
)
if not defined TAG (
    echo ERROR: version.txt not found or empty.
    exit /b 1
)

if not defined IMAGE_NAMESPACE set "IMAGE_NAMESPACE=ronhoebe"
set "APP_IMAGE=camping_trip"

if "%IMAGE_NAMESPACE%"=="" (
    set "APP_FULL=%APP_IMAGE%"
) else (
    set "APP_FULL=%IMAGE_NAMESPACE%/%APP_IMAGE%"
)

pushd "%SCRIPT_DIR%" >nul
if errorlevel 1 exit /b 1

echo Building %APP_FULL%:%TAG% and latest
docker build -t %APP_IMAGE%:latest -t %APP_IMAGE%:%TAG% -t %APP_FULL%:latest -t %APP_FULL%:%TAG% %* .
if errorlevel 1 goto :fail

popd >nul
echo Done.
endlocal & exit /b 0

:fail
set "EXITCODE=%ERRORLEVEL%"
popd >nul
endlocal & exit /b %EXITCODE%
