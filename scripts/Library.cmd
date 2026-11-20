@echo off
rem Opens the library catalog in the browser. If the library server isn't running, it is started
rem in the background, where it keeps running ("Stop library server" stops it). Only one ever runs.
uv run --project "%~dp0.." hylly --here serve --background %*
if errorlevel 1 pause
