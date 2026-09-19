@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>nul
if %errorlevel%==0 (
  start "" http://127.0.0.1:7530/
  python server.py
) else (
  start "" http://127.0.0.1:7530/
  py -3 server.py
)
