@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

REM ============================================================
REM  multiagent-tg :: setup
REM  Ставит uv + Python 3.11 + Python-зависимости.
REM  LLM по умолчанию - Google Gemini (никакой локальной модели не нужно).
REM ============================================================

pushd "%~dp0"

echo.
echo ============================================================
echo  multiagent-tg :: setup
echo ============================================================
echo.

REM --- 1. uv -----------------------------------------------------
where uv >nul 2>nul
if errorlevel 1 (
    echo [1/2] uv не найден. Устанавливаю через PowerShell...
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 (
        echo [ERROR] Не удалось установить uv.
        echo Установите вручную с https://docs.astral.sh/uv/getting-started/installation/
        pause
        popd
        exit /b 1
    )
    REM uv ставится в %USERPROFILE%\.local\bin - добавим в PATH этой сессии
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
) else (
    echo [1/2] uv уже установлен.
)

REM --- 2. Python + зависимости через uv sync ---------------------
echo [2/2] Устанавливаю Python 3.11 и зависимости (1-3 минуты)...
uv sync
if errorlevel 1 (
    echo [ERROR] uv sync не сработал. Смотрите вывод выше.
    pause
    popd
    exit /b 1
)

echo.
echo ============================================================
echo  setup готов!
echo  Дальше запускайте: start.bat
echo.
echo  Если хотите использовать локальную LLM вместо Gemini:
echo  - установите Ollama с https://ollama.com/download
echo  - запустите: ollama pull qwen2.5:7b
echo  - при первом start.bat выберите 'ollama' в мастере
echo ============================================================
echo.
REM Если нас вызвал start.bat - не делаем pause, продолжаем оттуда.
if "%CALLED_FROM_START%"=="1" goto :end
pause
:end
popd
endlocal
