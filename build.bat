@echo off
setlocal EnableExtensions
set "SRC=%~dp0"
set "WORK=%LOCALAPPDATA%\MultiZipBuild"
set "LOG=%WORK%\build.log"
echo.
echo ==== MultiZip builder ====
echo.

REM ---------- 0. checks
if not exist "%SRC%multizip.py" goto :nosource
if not exist "%WORK%" mkdir "%WORK%" >nul 2>&1
if not exist "%WORK%" goto :nowork
echo MultiZip build log > "%LOG%"

REM A running MultiZip.exe locks the file and causes "Access is denied" - close it first.
taskkill /f /im MultiZip.exe >nul 2>&1

REM ---------- 1. build in a private folder that is always writable
REM (builds inside Documents/Desktop/OneDrive/Program Files are what trigger Access denied)
echo Building in: %WORK%
copy /y "%SRC%multizip.py" "%WORK%\multizip.py" >nul
if errorlevel 1 goto :copyfail
if exist "%SRC%installer.iss" copy /y "%SRC%installer.iss" "%WORK%\installer.iss" >nul
REM the licence files go into the installer
if exist "%SRC%LICENSE" copy /y "%SRC%LICENSE" "%WORK%\LICENSE" >nul
if exist "%SRC%THIRD-PARTY-NOTICES.md" copy /y "%SRC%THIRD-PARTY-NOTICES.md" "%WORK%\THIRD-PARTY-NOTICES.md" >nul
if exist "%SRC%licenses" xcopy /y /i /q "%SRC%licenses" "%WORK%\licenses" >nul
cd /d "%WORK%"
if exist "dist\MultiZip.exe" del /f /q "dist\MultiZip.exe" >nul 2>&1

REM ---------- 2. find Python - 3.13 / 3.12 first: brand-new versions get only an old, unsafe py7zr
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do if not defined PY py -%%V --version >nul 2>&1 && set "PY=py -%%V"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython
set "PYMINOR="
for /f "usebackq delims=" %%v in (`%PY% -c "import sys; print(sys.version_info[1])"`) do set "PYMINOR=%%v"
if not defined PYMINOR set "PYMINOR=0"
echo Using Python 3.%PYMINOR%
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :notk
if %PYMINOR% GEQ 14 echo NOTE: very new Python - some optional packages may be skipped, that is OK.

REM ---------- 3. private virtual environment (no admin rights needed, system Python untouched)
set "VPY=%WORK%\venv\Scripts\python.exe"
REM a venv made with another Python version is made again
if exist "%VPY%" for /f "usebackq delims=" %%v in (`call "%VPY%" -c "import sys; print(sys.version_info[1])"`) do if not "%%v"=="%PYMINOR%" rmdir /s /q "%WORK%\venv" >nul 2>&1
if not exist "%VPY%" if exist "%WORK%\venv" rmdir /s /q "%WORK%\venv" >nul 2>&1
if not exist "%VPY%" %PY% -m venv "%WORK%\venv" >>"%LOG%" 2>&1
if exist "%VPY%" goto :havevenv
echo NOTE: could not create a virtual environment - installing packages for your user instead.
set "PYX=%PY%"
set "PIPX=--user"
goto :afterenv
:havevenv
set PYX="%VPY%"
set "PIPX="
:afterenv

echo.
echo [1/4] Installing PyInstaller ...
%PYX% -m pip install %PIPX% --upgrade pyinstaller >>"%LOG%" 2>&1
if errorlevel 1 goto :pipfail

REM ---------- 4. Pillow is required, the rest are optional (skipped if they cannot install)
echo.
echo [2/4] Installing packages ...
set "OPT="
%PYX% -m pip install %PIPX% --upgrade pillow >>"%LOG%" 2>&1
if errorlevel 1 goto :pilfail
%PYX% -c "from PIL import Image, ImageTk, ImageOps, ImageFont" >>"%LOG%" 2>&1
if errorlevel 1 goto :pilfail
REM Pillow's own PyInstaller hook brings the image formats; AVIF (4 MB) is never used by MultiZip.
set "OPT=%OPT% --exclude-module PIL._avif --exclude-module PIL.AvifImagePlugin"
call :getpy7zr
call :optional rarfile rarfile
REM hook mode: the drag-and-drop library for this PC only, not for every platform
call :optional tkinterdnd2 tkinterdnd2 hook
REM Python 3.14+ has zstd built in; zstandard is only needed on older versions
if %PYMINOR% LSS 14 (call :optional zstandard zstandard) else set "OPT=%OPT% --exclude-module zstandard"
for %%M in (pyzstd pyppmd bcj inflate64 Cryptodome brotli multivolumefile texttable) do call :hidden %%M
REM py7zr only falls back to these when the standard library lacks them; they drag in setuptools
set "OPT=%OPT% --exclude-module setuptools --exclude-module pkg_resources"

REM ---------- 5. RAR extractor to bundle (WinRAR or 7-Zip, if installed)
echo.
echo [3/4] Bundling the 7-Zip engine (RAR, ISO, CAB, WIM, VHD, DMG ... support) ...
if not exist tools mkdir tools
set "PF1=%ProgramFiles%"
set "PF2=%ProgramFiles(x86)%"
set "PF3=%ProgramW6432%"
call :grab "%PF1%"
call :grab "%PF2%"
call :grab "%PF3%"
if exist "%APPDATA%\MultiZip\engine\7z.exe" copy /y "%APPDATA%\MultiZip\engine\7z.exe" tools\ >nul 2>&1
if exist "%APPDATA%\MultiZip\engine\7z.dll" copy /y "%APPDATA%\MultiZip\engine\7z.dll" tools\ >nul 2>&1
if not exist "tools\7z.exe" call :getengine
set "EXTRA="
if exist "tools\UnRAR.exe" set EXTRA=%EXTRA% --add-binary "tools\UnRAR.exe;."
if exist "tools\7z.exe" if exist "tools\7z.dll" set EXTRA=%EXTRA% --add-binary "tools\7z.exe;." --add-binary "tools\7z.dll;."
if not defined EXTRA echo       no engine bundled - the app can fetch it later from Help ^> Supported formats

REM ---------- 6. build the exe (retries, because antivirus can briefly lock the new file)
echo.
echo [4/4] Building MultiZip.exe - takes a minute or two ...
set "TRY=0"
:pyi
set /a TRY+=1
%PYX% -m PyInstaller --noconfirm --clean --onefile --windowed --name MultiZip --distpath "%WORK%\dist" --workpath "%WORK%\pybuild" --specpath "%WORK%" %OPT% %EXTRA% multizip.py >>"%LOG%" 2>&1
if not errorlevel 1 if exist "%WORK%\dist\MultiZip.exe" goto :pyidone
if %TRY% GEQ 3 goto :pyifail
echo       attempt %TRY% failed - waiting a few seconds and trying again ...
ping -n 6 127.0.0.1 >nul
goto :pyi

:pyidone
echo       built OK

REM ---------- 7. copy the finished exe next to build.bat (fall back to the work folder if blocked)
if not exist "%SRC%dist" mkdir "%SRC%dist" >nul 2>&1
copy /y "%WORK%\dist\MultiZip.exe" "%SRC%dist\MultiZip.exe" >nul 2>&1
if errorlevel 1 goto :copyout
set "FINAL=%SRC%dist\MultiZip.exe"
goto :installer
:copyout
set "FINAL=%WORK%\dist\MultiZip.exe"
echo       NOTE: this folder is write-protected, so the app was saved here instead:
echo             %FINAL%
start "" explorer "%WORK%\dist"

REM ---------- 8. optional installer (needs Inno Setup)
:installer
set "ISCC="
if exist "%PF2%\Inno Setup 6\ISCC.exe" set "ISCC=%PF2%\Inno Setup 6\ISCC.exe"
if exist "%PF1%\Inno Setup 6\ISCC.exe" set "ISCC=%PF1%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC goto :noinno
if not exist "%WORK%\installer.iss" goto :noinno
echo       building installer ...
"%ISCC%" installer.iss >>"%LOG%" 2>&1
if errorlevel 1 goto :innofail
if not exist "%SRC%Output" mkdir "%SRC%Output" >nul 2>&1
copy /y "%WORK%\Output\MultiZip-Setup.exe" "%SRC%Output\MultiZip-Setup.exe" >nul 2>&1
if errorlevel 1 echo       installer saved in: %WORK%\Output
if not errorlevel 1 echo       created Output\MultiZip-Setup.exe
goto :done

:noinno
echo       Inno Setup not installed - skipping installer. Optional: https://jrsoftware.org/isdl.php
goto :done

:innofail
echo.
echo WARNING: the installer step failed, but the app itself built fine.
call :showlog
goto :done

:done
copy /y "%LOG%" "%SRC%build.log" >nul 2>&1
echo.
echo ==== SUCCESS ====
echo Your app:  %FINAL%
echo (double-click it, no install needed)
echo.
pause
exit /b 0

REM ---------- subroutines
:optional
%PYX% -m pip install %PIPX% --upgrade --only-binary=:all: %1 >>"%LOG%" 2>&1 || %PYX% -m pip install %PIPX% --upgrade %1 >>"%LOG%" 2>&1
%PYX% -c "import %2" >nul 2>&1 || goto :optfail
echo       %1 OK
if "%3"=="" set "OPT=%OPT% --collect-all %2"
exit /b 0
:optfail
echo       %1 could not be installed - building without it
exit /b 0

:hidden
%PYX% -c "import %1" >nul 2>&1 && set "OPT=%OPT% --hidden-import %1"
exit /b 0

REM py7zr before 0.20.2 has a known path-traversal bug. On a Python too new for current py7zr only such an
REM old one installs: then it is left out, and the bundled 7-Zip engine handles 7z files instead.
:getpy7zr
%PYX% -m pip install %PIPX% --upgrade --only-binary=:all: py7zr >>"%LOG%" 2>&1 || %PYX% -m pip install %PIPX% --upgrade py7zr >>"%LOG%" 2>&1
%PYX% -c "import py7zr" >nul 2>&1 || goto :nopy7zr
%PYX% -c "import py7zr, re, sys; sys.exit(tuple(int(x) for x in re.findall(r'\d+', py7zr.__version__)[:3]) < (0, 20, 2))" >nul 2>&1 || goto :oldpy7zr
echo       py7zr OK
set "OPT=%OPT% --collect-all py7zr"
exit /b 0
:oldpy7zr
echo       py7zr: only an old version with a known security bug installs on Python 3.%PYMINOR% -
echo       leaving it out, so 7z files use the 7-Zip engine. Python 3.13 gets a current py7zr.
set "OPT=%OPT% --exclude-module py7zr"
exit /b 0
:nopy7zr
echo       py7zr could not be installed - building without it
exit /b 0

:grab
if exist "%~1\WinRAR\UnRAR.exe" copy /y "%~1\WinRAR\UnRAR.exe" tools\ >nul 2>&1
if exist "%~1\7-Zip\7z.exe" copy /y "%~1\7-Zip\7z.exe" tools\ >nul 2>&1
if exist "%~1\7-Zip\7z.dll" copy /y "%~1\7-Zip\7z.dll" tools\ >nul 2>&1
exit /b 0

:getengine
echo       7-Zip not installed - downloading the free 7-Zip engine (about 2 MB) ...
%PYX% multizip.py --get-engine tools >>"%LOG%" 2>&1
if errorlevel 1 echo       could not download it - the app still builds; use Help ^> Supported formats later.
exit /b 0

:showlog
echo ---- last lines of build.log ----
powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 30"
echo ---------------------------------
exit /b 0

REM ---------- failures
:nosource
echo ERROR: multizip.py was not found next to build.bat.
echo Both files must be in the same folder. If you downloaded a zip, EXTRACT it first
echo (right-click, Extract All) - do not run build.bat from inside the zip.
goto :failnolog

:nowork
echo ERROR: could not create the work folder %WORK%
echo Windows is blocking writes there - check your antivirus / Controlled folder access.
goto :failnolog

:copyfail
echo ERROR: could not copy multizip.py into %WORK%
echo Controlled folder access or antivirus may be blocking it.
goto :fail

:nopython
echo ERROR: Python was not found.
echo Install Python 3.12 or 3.13 from https://www.python.org/downloads/
echo and TICK the box "Add python.exe to PATH" on the first installer screen.
goto :fail

:notk
echo ERROR: this Python was installed without Tcl/Tk, which the app window needs.
echo Re-run the Python installer, choose Modify, and tick "tcl/tk and IDLE".
goto :fail

:pipfail
echo ERROR: could not install PyInstaller - check your internet connection.
call :showlog
goto :fail

:pilfail
echo ERROR: could not install Pillow, which is needed for JPG/WEBP backgrounds and fonts.
call :showlog
goto :fail

:pyifail
echo ERROR: PyInstaller failed to build the exe.
echo If the log says "Access is denied": your antivirus is blocking the new exe.
echo Pause it for a minute, or exclude this folder, then run build.bat again:
echo   %WORK%
call :showlog
goto :fail

:fail
copy /y "%LOG%" "%SRC%build.log" >nul 2>&1
:failnolog
echo.
echo BUILD FAILED. Full details are in: %LOG%
echo.
pause
exit /b 1
