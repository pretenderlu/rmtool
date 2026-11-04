@echo off
if exist "%~dp0build\.venv\Scripts\pythonw.exe" (
    start "" "%~dp0build\.venv\Scripts\pythonw.exe" "%~dp0rmtool.py"
) else (
    py -3.12 "%~dp0rmtool.py"
)
