@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

REM ============================================================
REM  multiagent-tg :: start (главная кнопка — делает всё)
REM ============================================================
REM Логика:
REM  - если первый запуск (нет .venv) → автоматически зовёт setup.bat
REM  - если .env нет → мастер настройки
REM  - если нет залогиненных сессий → login
REM  - если group_id не задан → подсказывает где взять
REM  - иначе → запускает агентов
REM ============================================================

pushd "%~dp0"

echo.
echo ============================================================
echo  multiagent-tg :: start
echo ============================================================
echo.

REM --- первый запуск: setup ------------------------------------
if not exist ".venv" (
    echo Первый запуск - запускаю setup ^(установка uv, Python, зависимостей, модели^)...
    echo.
    set "CALLED_FROM_START=1"
    call "%~dp0setup.bat"
    set "CALLED_FROM_START="
    if errorlevel 1 (
        echo [ERROR] setup упал.
        pause
        popd
        exit /b 1
    )
)

REM --- если uv пропал из PATH (не открыли новый cmd) ------------
where uv >nul 2>nul
if errorlevel 1 (
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

REM --- .env -----------------------------------------------------
if not exist ".env" (
    echo .env не найден - запускаю мастер настройки...
    uv run multiagent-tg init
    if errorlevel 1 (
        echo [ERROR] Мастер прервался.
        pause
        popd
        exit /b 1
    )
)

REM --- проверка LLM ---------------------------------------------
echo.
echo Проверяю что LLM отвечает...
uv run multiagent-tg doctor
if errorlevel 1 (
    echo.
    echo [WARN] LLM не отвечает. Возможные причины:
    echo  - неверный LLM_API_KEY в .env ^(Gemini: https://aistudio.google.com/app/apikey^)
    echo  - если выбрали Ollama: она не запущена или модель не скачана
    echo  - нет доступа в интернет
    echo Продолжать? Если LLM не работает, агенты будут молчать.
    pause
)

REM --- проверим что хоть кто-то залогинен ------------------------
if not exist "sessions\sveta.session" (
    echo.
    echo [!] Похоже, ни один аккаунт не залогинен.
    echo Запущу login - нужно будет ввести SMS-код для каждого номера.
    echo.
    uv run multiagent-tg login
    if errorlevel 1 (
        echo [ERROR] login прервался.
        pause
        popd
        exit /b 1
    )
)

REM --- предупреждение про group id ------------------------------
findstr /b "TG_GROUP_ID=0" .env > nul
if not errorlevel 1 (
    echo.
    echo ============================================================
    echo  [!] Нужен ещё один шаг
    echo ============================================================
    echo  В .env пока TG_GROUP_ID=0 - агент не знает, в какую группу писать.
    echo.
    echo  1^) Зайдите в Telegram со своего основного аккаунта
    echo  2^) Создайте новую группу
    echo  3^) Добавьте туда Свету ^(@её_username, который вы видели после login^)
    echo  4^) Запустите groupid.bat - он покажет id группы
    echo  5^) Откройте .env в блокноте и замените TG_GROUP_ID=0 на полученный id
    echo  6^) Снова запустите start.bat
    echo.
    pause
    popd
    exit /b 1
)

REM --- поехали --------------------------------------------------
echo.
echo ============================================================
echo  Запускаю агентов. Для остановки - Ctrl+C.
echo ============================================================
uv run multiagent-tg run

popd
endlocal
