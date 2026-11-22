@echo off
cd /d "%~dp0"
python -m vmagent serve-http --port %1
