---
name: set-up-server
description: "Set up a Hylly library and its server for the owner, or check one that is set up: installing it, the library folder, the first user, the catalog, the server and its start at logon, letting people in, a second computer, and backups. Use when asked to \"set up Hylly\", \"install Hylly on my server\", \"let my family in\", \"reach my library from outside\", \"set up backups\", or \"is my Hylly set up right?\"."
---

# Setting up a Hylly server

The steps themselves are in `docs/playbooks.md`, which any assistant reads, and the rules in `AGENTS.md`: this skill is only the order to take them in, and the check of `hylly doctor --json` that proves each one. Read `AGENTS.md` first, then each playbook as its step comes. Where this page and a playbook differ, the playbook wins: tests hold it to the code.

## Rules (no exceptions)

1. **Secrets are the owner's:** every password, login, key and one-time link, and the alarms' ntfy address. They run `hylly user add NAME`, `hylly login SITE` and `hylly user link NAME` in their own terminal ([What is the owner's](../../../docs/playbooks.md#what-is-the-owners)).
2. **The system is the owner's:** sudo and `HYLLY_ALLOW_ROOT` they type, always. A start at logon, a service or a timer only once they've said yes, and whatever it takes they type themselves: sudo, and the commands `hylly server autostart on` prints on Linux and macOS.
3. **A yes is the owner's:** no `--yes` before they've seen the plan or the `--dry-run` and said yes to it (`hylly init` on a new folder, a first backup to a disk, `hylly user role`).
4. **Never widen access unasked:** where the server listens, the address people use, the proxies it trusts, Tailscale Serve or Funnel, a router port, ssh keys and `[remote]`, a new user or a role. Show the owner the settings' lines before they go in.
5. **Never `hylly init` to get past a refusal:** code 5 with "No Hylly library at ..." means a disk or a share isn't up ([The library isn't there](../../../docs/playbooks.md#the-library-isnt-there)).
6. **Look first, and prove after:** `hylly doctor --json` before the first step and after each one, by the check the table names, never by a command having ended well alone.
7. **Try things on the demo,** never on the owner's real server: `hylly demo --background`, with its variables set and `HYLLY_SETTINGS`, `HYLLY_RULES` and `HYLLY_REMOTE` unset ([Try things apart from the real library](../../../docs/playbooks.md#try-things-apart-from-the-real-library)). End only a process you started yourself.

## Method

Each step's playbook says what to ask first, the commands in order, and what is the owner's alone. A check named below is an `id` of `hylly doctor --json`.

| Step | Do | Done when |
|---|---|---|
| 1. Where | [Where am I running](../../../docs/playbooks.md#where-am-i-running): which computer, which settings and which library a command reaches | `version`, `places` and `library` say what the owner expects |
| 2. Ask | The *Ask first* of [First setup](../../../docs/playbooks.md#first-setup): the computer that keeps the library and runs its server, the library's folder, and whether Hylly is there already | The owner has answered |
| 3. Install | [First setup](../../../docs/playbooks.md#first-setup), its steps 1 and 2: the program, and 7-Zip for CBR comics, which the owner installs | `version` and `pdfs`, and `7-zip` with its RAR part |
| 4. The library | Its step 3: `hylly init FOLDER --dry-run`, shown to the owner, then `hylly init FOLDER` | `library` is `ok` |
| 5. The owner | Its step 4: the owner runs `hylly user add NAME` | `access`, with one or more `owners` |
| 6. The catalog and the logins | Its steps 5 and 6: `hylly catalog`; the owner logs in to each shop they use | `catalog` is `ok`, and `logins` lists each login as saved |
| 7. The server | Its steps 7 and 8: `hylly serve --background`, and its start at logon once the owner has said yes; on Linux and macOS, `hylly server autostart on` prints the unit or the agent and the commands that install it, which are the owner's alone to run | `server` is running, and `logon-start` has no problem |
| 8. People in | [Letting people in](../../../docs/playbooks.md#letting-people-in), one step further out at a time, each the owner's choice | `access` for each user and address, and `walls` and `alarms` for anything past the home network |
| 9. A second computer | [A second computer](../../../docs/playbooks.md#a-second-computer), where the owner types commands on another one | `hylly -H HOST doctor --json` has the server's own `version` and `library`, and there is no `remote` problem |
| 10. Backups | [Backup and restore](../../../docs/playbooks.md#backup-and-restore): a disk other than the library's, and a timer if the owner wants one | `backup`, with the day and the target |

## Checklist at the end

- [ ] `hylly doctor --json` exits with 0, and no check is a `problem`.
- [ ] Each step was proved by its check, not by its command having ended well alone.
- [ ] Every password, login and key was typed by the owner, in their own terminal.
- [ ] Nothing reaches the library from further out than the owner has said yes to.
- [ ] The owner knows what stays theirs: the logins when a shop asks again, the backup disk, and updates (the help's *Keeping it up to date*).

## When it breaks later

1. `hylly doctor --json`: the `next` of each check that isn't `ok`, and its `run` where it gives one ([Look first](../../../docs/playbooks.md#look-first)).
2. A library that isn't there: [The library isn't there](../../../docs/playbooks.md#the-library-isnt-there).
3. A job that seems stuck: [A job seems stuck](../../../docs/playbooks.md#a-job-seems-stuck).
4. After an update: the help's *Keeping it up to date*, then `hylly doctor --json` again.
