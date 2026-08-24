@echo off
chcp 65001 >nul
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python check_config.py
pause
