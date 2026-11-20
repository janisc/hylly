"""python -m hylly: the same as the hylly command (the logon start on Windows and the server page's Restart use this;
the server itself is python -m hylly.daemon, hyllyd)."""

from .cli import main

main()
