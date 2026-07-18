@echo off
setlocal
call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b 1

set "PATH=C:\Users\hunsi\AppData\Local\Microsoft\WinGet\Packages\Ninja-build.Ninja_Microsoft.Winget.Source_8wekyb3d8bbwe;%PATH%"
set "VCPKG_ROOT=C:\vcpkg"

cd /d "C:\Users\hunsi\Desktop\RME-SaintsOT"
cmake --build --preset windows-release
