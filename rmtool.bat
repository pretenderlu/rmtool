@echo off
chcp 65001 > nul
echo 正在启动 reMarkable 管理工具...
python -W ignore::DeprecationWarning rmtool.py
pause
