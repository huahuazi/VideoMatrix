@echo off
cd /d "%~dp0"
python -m vmagent serve-mcp --ndjson %*
