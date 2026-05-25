@echo off
chcp 65001 > nul
pushd "%~dp0"
echo.
echo ============================================================
echo  multiagent-tg :: group-id (поиск id группы)
echo ============================================================
echo Покажет все ваши диалоги через аккаунт Светы.
echo Найдите вашу группу - id рядом с её именем впишите в .env как TG_GROUP_ID.
echo.
uv run multiagent-tg group-id sveta
echo.
pause
popd
