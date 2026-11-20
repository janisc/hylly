"""hyllyd: the library server itself, and only that. It takes what starting the server needs (commands.daemon_parser)
and serves until it's stopped; everything else is hylly's: hylly serve starts it for a person, hylly server status, stop
and restart look after it, and a service starts it as hyllyd --in-background (examples/hylly.service, and the units
hylly server autostart on prints). It never opens a browser.

It is in the server's part (parts.py), and imports nothing else: the shops' logins, the code that talks to the shops
and the lookups aren't even loaded (tests/test_parts.py starts it and looks)."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from . import paths
from .exits import INTERRUPTED, LOCAL, NOT_SET_UP, USAGE, Exit, bug, hide_addresses_in_tracebacks


def main(argv: list[str] | None = None) -> None:
    """The server, with the options given, and how it ends: exits.TABLE, as for hylly. What it says on standard error,
    the reason it couldn't start too, also goes into server.log (instance.logged), as the one place a hidden server's
    words can be found."""
    from . import db, instance
    from .commands import daemon_parser

    hide_addresses_in_tracebacks()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = daemon_parser().parse_args(sys.argv[1:] if argv is None else argv)
    with instance.logged() if not args.dry_run else contextlib.nullcontext():  # a dry run starts nothing: nothing to log
        try:
            try:
                serve(args)
            except (db.NotHere, db.Newer, db.Unusable, db.NotKept) as e:  # the database: what's wrong, and where to go
                raise Exit(NOT_SET_UP, str(e)) from None
            except OSError as e:  # with its file's name: a folder it may not write, say
                raise Exit(LOCAL, f"Can't go on: {e}") from None
            except KeyboardInterrupt:
                raise Exit(INTERRUPTED, "Stopped.") from None
            except Exception as e:  # noqa: BLE001 - what nothing foresaw: a bug, exit code 70 with its traceback, as for hylly
                raise bug(e) from None
        except Exit as e:
            if e.message:
                print(e.message, file=sys.stderr, flush=True)
            raise


def serve(args) -> None:
    """--in-background: the server nobody watches, which hylly serve --background, a restart and the services start, with
    the settings as its only options; without it, the server here, in the foreground, with what was typed for this one
    run. Either way, on a computer whose server is a system-wide service with walls, only inside them."""
    from . import instance, rules, settings
    from . import serve as server

    if paths.running_as_root():
        raise Exit(NOT_SET_UP, paths.AS_ROOT)
    if not args.dry_run:  # which changes nothing
        paths.keep_private()
        settings.secrets_at_start()  # the folder for the alarms' secrets, made if it isn't there
    extra = [str(Path(p).absolute()) for p in args.rules] if args.rules else None  # by their full paths, as given
    typed = {k: v for k, v in (("port", args.port), ("lan", args.lan),
                                ("library", str(Path(args.library).absolute()) if args.library else None), ("rules", extra)) if v is not None}
    if instance.system_service() and not instance.in_system_service():
        raise Exit(NOT_SET_UP, instance.WALLED)
    if args.in_background and typed:
        raise Exit(USAGE, f"A server in the background takes its options from the settings, so that a restart brings the same "
                          f"one back: --{', --'.join(typed)} can't be given with --in-background. Set them under [server] in "
                          f"{paths.settings_file()} (library and rules at the top). Nothing was started.")
    rules.EXTRA[:] = extra or []
    port, listen = server.options(typed)
    root = paths.library_root(args.library)
    if args.dry_run:
        return print(f"It would serve {root} on port {port}, listening on {listen}" + (", unwatched, its messages in "
                     f"{instance.log_file()} and on standard error" if args.in_background else "") + ". Nothing was started.")
    server.run(root, port=port, listen=listen, open_browser=False, background=args.in_background, given=typed)


if __name__ == "__main__":  # python -m hylly.daemon, as the background server and the logon start run it
    main()
