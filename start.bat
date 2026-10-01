@echo off
title Tiny Amazon Scout - close this window to stop
cd /d "%~dp0"
where python >nul 2>nul && (python tiny_amazon_scout.py & goto :eof)
where py >nul 2>nul && (py tiny_amazon_scout.py & goto :eof)
echo Python was not found. Install it from python.org and tick "Add python.exe to PATH".
pause
