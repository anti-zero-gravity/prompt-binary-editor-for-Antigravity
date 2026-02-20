@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Binary Prompt Editor Server
echo ---------------------------------------------------
echo Binary Prompt Editor を起動しています...
echo ---------------------------------------------------
echo.
echo ブラウザを起動します: http://localhost:5000
start "" "http://localhost:5000"
echo.
echo サーバーログ:
echo (停止するにはこのウィンドウを閉じてください)
echo ---------------------------------------------------
python app.py
if %errorlevel% neq 0 (
    echo.
    echo エラーが発生しました。'python' コマンドが見つからない可能性があります。
    echo 'py' コマンドで再試行します...
    py app.py
)
pause
