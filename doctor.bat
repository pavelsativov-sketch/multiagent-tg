@echo off
chcp 65001 > nul
pushd "%~dp0"
echo.
echo ============================================================
echo  multiagent-tg :: doctor (проверка LLM)
echo ============================================================
uv run multiagent-tg doctor
echo.
pause
popd
