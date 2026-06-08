@echo off
title CoC2 Reader
where py >nul 2>nul && (py server.py & goto :eof)
where python >nul 2>nul && (python server.py & goto :eof)
echo Python not found. Install Python 3 from https://www.python.org/ and try again.
pause