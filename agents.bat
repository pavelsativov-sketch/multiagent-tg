@echo off
chcp 65001 > nul
pushd "%~dp0"
uv run multiagent-tg agents
pause
popd
