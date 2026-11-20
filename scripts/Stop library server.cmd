@echo off
rem Stops the library server. Library.cmd starts it again, and so does the next logon if
rem "hylly server autostart on" was used.
uv run --project "%~dp0.." hylly --here server stop %*
pause
