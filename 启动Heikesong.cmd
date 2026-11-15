@echo off
setlocal
pushd "%~dp0" || exit /b 1
if not exist "P4\start.cmd" (
    echo Cannot find P4\start.cmd. Keep this launcher in the project root.
    pause
    popd
    exit /b 2
)
call "P4\start.cmd" %*
set "heikesong_launch_result=%errorlevel%"
popd
exit /b %heikesong_launch_result%
