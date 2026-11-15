@echo off
rem Double-click to open the Chat Spike Clipper dashboard.
rem Keep this window open while you use it; closing it stops the dashboard.
cd /d "%~dp0"

where clipper >nul 2>nul && (clipper ui %* & goto :end)

rem PATH not refreshed yet (e.g. right after installing Python): try the usual places.
for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do if exist %%P (%%P -m clipper ui %* & goto :end)

where py >nul 2>nul && (py -3 -m clipper ui %* & goto :end)

echo Could not find Python with the clipper installed.
echo Install Python 3.10+ and run:  pip install -e ".[plot,facecam,live]"

:end
pause
