@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -B -X utf8 scripts\wechat_stack\manage.py start
set "stack_exit=%errorlevel%"
if /i not "%~1"=="--no-pause" pause
exit /b %stack_exit%
