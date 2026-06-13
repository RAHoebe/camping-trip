@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "TAG=%~1"
if not defined TAG (
    if exist "%SCRIPT_DIR%\version.txt" (
        set /p TAG=<"%SCRIPT_DIR%\version.txt"
    )
)
if not defined TAG (
    echo ERROR: No tag provided and version.txt not found or empty.
    echo Usage: %~nx0 [tag]
    exit /b 1
)

pushd "%SCRIPT_DIR%" >nul
if errorlevel 1 (
    echo ERROR: Failed to change directory to %SCRIPT_DIR%
    exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
    echo ERROR: git was not found in PATH.
    goto :fail
)

where gh >nul 2>nul
if errorlevel 1 (
    echo ERROR: GitHub CLI ^(gh^) was not found in PATH.
    echo Install it and run: gh auth login
    goto :fail
)

git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
    echo ERROR: This directory is not a git repository.
    goto :fail
)

git rev-parse --abbrev-ref --symbolic-full-name @{u} >nul 2>nul
if errorlevel 1 (
    echo ERROR: Current branch has no upstream branch configured.
    echo Set one with: git push -u origin HEAD
    goto :fail
)

git update-index -q --refresh
set "DIRTY="
for /f "usebackq delims=" %%A in (`git status --porcelain`) do (
    set "DIRTY=1"
)
if defined DIRTY (
    echo ERROR: Working tree has uncommitted changes. Commit or stash them before releasing.
    goto :fail
)

echo Fetching latest remote refs...
git fetch --prune
if errorlevel 1 goto :fail

set "AHEAD="
set "BEHIND="
for /f "tokens=1,2" %%A in ('git rev-list --left-right --count HEAD...@{u}') do (
    set "AHEAD=%%A"
    set "BEHIND=%%B"
)

if not "%AHEAD%"=="0" (
    echo ERROR: Local branch is %AHEAD% commit^(s^) ahead of upstream. Push your commits first.
    goto :fail
)

if not "%BEHIND%"=="0" (
    echo ERROR: Local branch is %BEHIND% commit^(s^) behind upstream. Pull or rebase first.
    goto :fail
)

git rev-parse -q --verify "refs/tags/%TAG%" >nul 2>nul
if not errorlevel 1 (
    echo ERROR: Local tag %TAG% already exists.
    goto :fail
)

git ls-remote --exit-code --tags origin "refs/tags/%TAG%" >nul 2>nul
if not errorlevel 1 (
    echo ERROR: Remote tag %TAG% already exists on origin.
    goto :fail
)

gh release view "%TAG%" >nul 2>nul
if not errorlevel 1 (
    echo ERROR: GitHub release %TAG% already exists.
    goto :fail
)

echo Creating tag %TAG%...
git tag -a "%TAG%" -m "Release %TAG%"
if errorlevel 1 goto :fail

echo Pushing tag %TAG% to origin...
git push origin "%TAG%"
if errorlevel 1 goto :fail_after_tag

echo Creating GitHub release %TAG%...
gh release create "%TAG%" --verify-tag --title "%TAG%" --generate-notes
if errorlevel 1 goto :fail_after_push

popd >nul
echo Done. GitHub release %TAG% created.
endlocal & exit /b 0

:fail_after_push
echo ERROR: Release creation failed. The tag was pushed to origin.
goto :fail

:fail_after_tag
echo ERROR: Push failed. The local tag %TAG% was created but not pushed.
goto :fail

:fail
set "EXITCODE=%ERRORLEVEL%"
if "%EXITCODE%"=="0" set "EXITCODE=1"
popd >nul
endlocal & exit /b %EXITCODE%
