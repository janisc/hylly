"""Telling you when a long job stops on its own: a notification on this computer (Windows' action centre, macOS's
Notification Centre, a Linux desktop's). Without one, as on a server with no screen, the log line the job writes anyway
is all there is."""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import sys

from .paths import windows_tool

# Windows PowerShell (5.1) can use the notification API itself; its own app id is one Windows already knows.
SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:HYLLY_NOTE_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:HYLLY_NOTE_TEXT)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


# Where Homebrew puts terminal-notifier, looked in as well: a server started at logon (launchd) has no PATH that has them
MAC_TOOLS = ("/opt/homebrew/bin", "/usr/local/bin")


def mac_notifier() -> str:
    """terminal-notifier, where it's installed (brew install terminal-notifier); "" where it isn't."""
    return shutil.which("terminal-notifier", path=os.pathsep.join([os.environ.get("PATH", ""), *MAC_TOOLS])) or ""


def _free(value: str) -> str:
    """A text for terminal-notifier, which takes one that starts with a dash or a bracket for an option of its own unless
    a backslash comes first."""
    return "\\" + value if value[:1] in ("-", "[", "\\") else value


def command(title: str, text: str, platform: str = sys.platform, link: str = "", notifier: str | None = None) -> list[str]:
    """What shows a notification here. The texts go as separate arguments or, on Windows, in the environment: never in
    a script, as a name someone typed at the login page can be among them. On a Mac, terminal-notifier where it's
    installed (notifier: where; None, looked for), whose notification opens link when clicked; else AppleScript's, which
    macOS takes for Script Editor's, so a click on it opens Script Editor."""
    if platform == "win32":  # by its full path: by its name, Windows would look in the current folder first
        return [windows_tool("powershell"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", SCRIPT]
    if platform == "darwin":
        found = mac_notifier() if notifier is None else notifier
        if found:
            return [found, "-title", _free(title), "-message", _free(text), *(["-open", link] if link else [])]
        return ["osascript", "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run", title, text]
    return ["notify-send", "--app-name=Hylly", "--", title, html.escape(text, quote=False)]  # many read the text as markup


def toast(title: str, text: str, link: str = "") -> bool:
    """Shows a notification; False where it couldn't. link: what a click on it opens, where the system's way allows."""
    if os.environ.get("HYLLY_NO_NOTIFY"):
        return False
    title, text = f"Hylly: {title}"[:120], text[:400]  # on Windows it otherwise shows as coming from PowerShell
    try:
        done = subprocess.run(
            command(title, text, link=link),
            env={**os.environ, "HYLLY_NOTE_TITLE": title, "HYLLY_NOTE_TEXT": text},
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
