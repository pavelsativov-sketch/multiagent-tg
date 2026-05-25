@echo off
chcp 65001 > nul
pushd "%~dp0"
echo.
echo ============================================================
echo  multiagent-tg :: login (вход в Telegram-аккаунты)
echo ============================================================
echo Для каждого включённого агента запросит SMS-код / пароль 2FA.
echo.
uv run multiagent-tg login
echo.
pause
popd
