"""Every file or folder of the program that the public docs name is in what's published: a pointer to a file that has
gone, or to one kept out of what's published, can't stay in them unnoticed. What's published is what git archive would
give from this checkout: the files git keeps or would take in, less what's marked export-ignore (a folder marked so takes
everything in it along). Without git, as in an unpacked archive, it's the files that are there."""

from __future__ import annotations

import functools
import html
import posixpath
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest

from helpers import REPO, git, kept, published

SKILLS = sorted(p.relative_to(REPO).as_posix() for p in (REPO / ".claude" / "skills").glob("*/SKILL.md"))  # every one
DOCS = ["README.md", "AGENTS.md", "CONTRIBUTING.md", "THIRD-PARTY-NOTICES.md", "settings.example.toml", "src/hylly/help.html",
        *SKILLS]  # and every published docs/*.md
STOP = "\\s`'\"<>()\\[\\]{},;:|"  # what ends a path in running text
PLACEHOLDER = re.compile(r"[*…]|NNNN|\.\.\.")  # a part of a path that stands for many: src/hylly/migrations/NNNN_*.sql


def there(path: str) -> bool:
    path = posixpath.normpath(path.replace("\\", "/"))
    files = published()
    return (REPO / path).exists() if files is None else path in files


@functools.cache
def tops() -> tuple[frozenset[str], frozenset[str]]:
    """The names at the top that a path into the program starts with, folders and files: what git keeps there, published
    or not, and what .gitignore keeps out by name (a folder or file of your own that is never published)."""
    files = kept()
    if files is None:
        folders = {p.name for p in REPO.iterdir() if p.is_dir()}
        names = {p.name for p in REPO.iterdir() if p.is_file()}
    else:
        folders = {f.split("/")[0] for f in files if "/" in f}
        names = {f for f in files if "/" not in f}
    for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines():
        own = re.fullmatch(r"/([^/*?\[\s]+)(/?)", line.strip())
        if own:
            (folders if own[2] else names).add(own[1])
    return frozenset(folders - {".git"}), frozenset(names)


def mentions(doc: str, text: str, folders, names) -> list[tuple[str, list[str]]]:
    """Each path into the program that the text names: as written, and what it may be read as. A Markdown link is
    read from its doc's folder; a path in running text from the top, with / or \\, up to the first space or, as a
    launcher's name has spaces, up to one of the next few words. A placeholder (NNNN_*.sql) leaves its folder."""
    found = []
    if doc.endswith(".md"):
        for target in re.findall(r"\]\(([^)\s]*)\)", text):
            if target and not re.match(r"[a-z][a-z+.-]*:|#", target):
                path = unquote(target.split("#")[0])
                found.append((target, [posixpath.normpath(posixpath.join(posixpath.dirname(doc), path))]))
        text = re.sub(r"\]\([^)\s]*\)", "]()", text)
    if doc.endswith(".html"):
        text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    start = r"(?<![\w.\\/%~$:-])"
    top = "|".join(re.escape(f) for f in sorted(folders, key=len, reverse=True))
    files = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    rx = re.compile(rf"{start}(?:(?:{top})[/\\][^{STOP}]*|(?:{files})(?![\w/\\-]|\.\w))" if files else
                    rf"{start}(?:{top})[/\\][^{STOP}]*")
    for hit in rx.finditer(text):
        written = hit[0].rstrip(".'")
        readings = [written]
        more = re.match(rf"(?: [^{STOP}/\\]+){{1,5}}", text[hit.end():])
        if more and not PLACEHOLDER.search(written):
            words = more[0].split(" ")[1:]
            readings += [f"{written} {' '.join(words[:n])}".rstrip(".") for n in range(1, len(words) + 1)]
        parts = re.split(r"[/\\]", written)
        cut = next((n for n, part in enumerate(parts) if PLACEHOLDER.search(part)), None)
        found.append((written, ["/".join(parts[:cut])] if cut else readings))
    return found


def everything() -> list[tuple[str, str, list[str]]]:
    files = published()
    shipped = (sorted(f for f in files if re.fullmatch(r"docs/[^/]+\.md", f)) if files is not None else
               sorted(p.relative_to(REPO).as_posix() for p in (REPO / "docs").glob("*.md")))
    folders, names = tops()
    return [(doc, written, readings) for doc in DOCS + shipped
            for written, readings in mentions(doc, (REPO / doc).read_text(encoding="utf-8"), folders, names)]


def test_every_path_the_public_docs_name_is_in_what_is_published():
    named = everything()
    assert len(named) > 40  # the docs name many of the program's files, and they're found
    missing = sorted({f"{doc}: {written}" for doc, written, readings in named if not any(map(there, readings))})
    assert not missing, "named in the docs, but not in what's published: " + "; ".join(missing)


def test_a_path_is_read_however_it_is_written():
    text = ("Double-click scripts\\Stop library server.cmd. See [the notes](more%20notes.md#top), `src/hylly/migrations/"
            "NNNN_*.sql`, docs/rules.md, scripts/ and README.md; not https://example.org/docs/x.md, a.docs/x or ~/docs/x.")
    found = dict(mentions("docs/a.md", text, {"docs", "scripts", "src"}, {"README.md"}))
    assert list(found) == ["more%20notes.md#top", "scripts\\Stop", "src/hylly/migrations/NNNN_*.sql", "docs/rules.md",
                           "scripts/", "README.md"]
    assert found["more%20notes.md#top"] == ["docs/more notes.md"]  # a link, from the doc's own folder
    assert "scripts\\Stop library server.cmd" in found["scripts\\Stop"]  # a name with spaces
    assert found["src/hylly/migrations/NNNN_*.sql"] == ["src/hylly/migrations"]  # a placeholder: its folder
    assert found["docs/rules.md"][0] == "docs/rules.md" and found["README.md"] == ["README.md"]
    page = mentions("x.html", "<code>docs\\playbooks.md</code> and <code>tools/a.py</code>", {"docs", "tools"}, set())
    assert [written for written, _ in page] == ["docs\\playbooks.md", "tools/a.py"]


def test_where_things_are_names_what_hylly_keeps_on_this_computer_and_stopping_says_what_stays_behind(tmp_path):
    """Each file Hylly keeps beside the settings, by the name the code gives it, has its row in Where things are,
    and Stopping using Hylly names each place a person who leaves would have to find, on Windows, Linux and macOS: the
    settings folder with the logins and keys, what long jobs said, the reader's cache, the demo, the jobs' timer, the way
    in from outside and the firewall's permission on Windows, the browsers. Nothing is deleted by Hylly itself: the
    section says what to remove."""
    from hylly import access, bell, db, helppage, hyllycopy, instance, paths, settings

    page = helppage.help_page(tmp_path, generic=True)
    where = re.search(r'<section id="where">.*?</section>', page, re.S)[0]
    stopping = re.search(r'<section id="stopping">.*?</section>', page, re.S)[0]
    kept = [db.FILE, f"{Path(db.FILE).stem}.before-", instance.log_file().name, access.access_log_file().name,
            bell.log_file().name, bell.state_file().name, bell.job_state_file().name, hyllycopy._targets_file().name,
            paths.logins_dir().name, settings.secrets_dir().name, "cache_folder"]
    assert [name for name in kept if name not in where] == []
    places = ["%LOCALAPPDATA%\\Hylly", "~/.local/share/Hylly", "HYLLY_HOME", "~/.local/state/hylly", "Hylly-jobs",
              "cache_folder", "hylly-demo", "hylly-jobs.timer", "tailscale funnel", "logins", "secrets", "db-backup",
              ".hyllycopy", "io.github.janisc.hylly.plist", "Hylly library server", "hylly.", "Allow an app through firewall"]
    assert [place for place in places if place not in stopping] == []
    assert 'href="#stopping"' in where and 'href="#where"' in stopping
    # Hylly's own folders by kind (paths.PLACES): each system's default for each, as the help writes it, and each kind's
    # own variable, in Where things are; the defaults of a new install's settings folder, on each system, in Stopping
    defaults = []
    for kind, (_, variable, _, systems) in paths.PLACES.items():
        assert variable in where, variable
        for system, template in systems.items():
            if template.startswith("<data>"):
                continue
            head, _, rest = template.partition("/")
            written = (f"{head}\\{rest.replace('/', chr(92))}" if system == "windows" else
                       f"~/{paths.XDG[head[1:]]}/{rest}" if head.startswith("$") else template)
            defaults.append(written)
            assert written in where, (kind, system, written)
    assert all(place in stopping for place in ("%APPDATA%\\Hylly", "~/.config/hylly", "~/Library/Application Support/Hylly",
                                               "~/.local/share/hylly", "~/.cache/hylly", "~/Library/Caches/Hylly"))
    assert len(defaults) == 12 and 'id="places"' in where and "hylly settings places" in where


def test_what_builds_tools_and_assistants_leave_behind_stays_out_of_git_and_nothing_git_keeps_is_ignored():
    """uv build's dist/, the audit's requirements.txt when its steps are run by hand, an assistant's working copies and
    settings, and what systems and editors leave beside the files: .gitignore itself leaves them out, not a computer's
    own exclude list, so git add -A takes none of them in any clone. And no file git keeps is one it says to leave out,
    whose changes would go unseen."""
    if kept() is None:
        pytest.skip("no git here")
    left = ["dist/hylly-0.1.0.tar.gz", "requirements.txt", ".claude/worktrees/a-fix/README.md",
            ".claude/settings.local.json", ".DS_Store", "docs/.DS_Store", "Thumbs.db", ".idea/workspace.xml",
            ".vscode/settings.json"]
    said = subprocess.run(["git", "check-ignore", "-v", "-z", "-n", "--no-index", "--stdin"], cwd=REPO,
                          input="\0".join(left), capture_output=True, text=True, encoding="utf-8").stdout.split("\0")
    by = {path: source for source, _, _, path in (said[i:i + 4] for i in range(0, len(said) - 3, 4))}
    assert {path: by.get(path) for path in left} == {path: ".gitignore" for path in left}
    assert git("ls-files", "-z", "--cached", "--ignored", "--exclude-per-directory=.gitignore") == ""


def test_line_endings_are_the_repository_s_whatever_a_computer_s_git_settings_say():
    """.gitattributes: LF in every checkout, but CRLF for the Windows scripts, and the pictures and fonts never taken
    for text; and every file git keeps is stored with LF, so no checkout or git add changes one by itself."""
    if kept() is None:
        pytest.skip("no git here")
    files = [f for f in git("ls-files", "-z").split("\0") if f]
    said = git("check-attr", "-z", "--stdin", "eol", "text", given="\0".join(files)).split("\0")
    attr = {(path, name): value for path, name, value in zip(said[0::3], said[1::3], said[2::3])}
    assert {f for f in files if attr[f, "eol"] == "crlf"} == {f for f in files if f.endswith(".cmd")} != set()
    assert {attr[f, "eol"] for f in files if not f.endswith(".cmd")} == {"lf"}
    pictures_and_fonts = [f for f in files if f.endswith((".png", ".woff2"))]
    assert pictures_and_fonts and {attr[f, "text"] for f in pictures_and_fonts} == {"unset"}
    stored = [line.split("\t", 1) for line in git("ls-files", "-z", "--eol").split("\0") if line]
    assert [path for info, path in stored if info.split()[0] not in ("i/lf", "i/-text", "i/none")] == []


@pytest.mark.parametrize("skill", SKILLS)
def test_a_skill_s_front_matter_reads_as_yaml_anywhere(skill):
    """GitHub, as any strict YAML reader, takes a ": " inside a plain value for a key of its own and shows the skill as
    an error: so its description is in double quotes (a quote or backslash inside escaped), and its name a plain word."""
    head = (REPO / skill).read_text(encoding="utf-8").split("\n---\n", 1)[0].split("\n")
    assert head[0] == "---"
    fields = dict(line.split(": ", 1) for line in head[1:])
    assert re.fullmatch(r"[a-z0-9-]+", fields["name"]) and re.fullmatch(r'"(?:[^"\\]|\\["\\])*"', fields["description"])
