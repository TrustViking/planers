@echo off
rem Regenerate requirements.txt from the planer venv. Never edit requirements.txt by hand.
setlocal
cd /d "%~dp0.."
call ".venv_planers\Scripts\activate.bat" || exit /b 1
python -m pip freeze > requirements.txt
endlocal
