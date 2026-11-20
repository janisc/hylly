# Ways to run it

Hylly runs on Windows, macOS and Linux: on the computer you read on, on a server at home, or open to the internet. Whichever way, two things stay the same:

- **Every request logs in, wherever it comes from:** this computer, the home network, a VPN, a tunnel or a proxy. Where a request comes from never lets it in. Only the login page and what it needs (its colours, the app's icons) answer without a login, and `/healthz`, which tells a health check `ok` and nothing more.
- **Each user's role is the same everywhere:** borrower, reader, librarian or owner. The first user you add is the owner, and the Server and Database pages are an owner's, from any device.

What changes from one way to the next is where the server listens (`listen` under `[server]` in the settings), the address people use (`address`), and what stands in front of the server to give it HTTPS. Below is a block of settings for each, with what to type and why. The help page says the same under *Ways to run it*, the rest of the login rules under *Logins*, and [settings.example.toml](../settings.example.toml) has every setting.

The commands below are written as `hylly ...`, the command the README's *Quick start* installs, run in any folder.

## First, whichever way

```
hylly user add NAME     # you: the first user is the owner
hylly serve             # at a terminal, opens this computer's browser logged in as the owner
```

- `hylly user add` asks for the password twice, 8 characters at least; a script or a container's setup gives it once with `--password-stdin`, from a pipe or a file others can't read. Every user after the first is a reader, unless `--role` says otherwise.
- `hylly serve` at a terminal, with or without `--background`, shows a one-time link that logs a browser in as the owner, and opens it where there is a browser to open: on Windows and macOS, or with a desktop (`DISPLAY` or `WAYLAND_DISPLAY` set), never over ssh to a server. A link works once, within 10 minutes. `hylly user link NAME` makes one for any user, at each address the library has. A server started out of sight (the hidden server itself, the start at logon, a system service) shows none, as its log would keep it.
- Every other browser shows the login page, once: it then stays logged in until 30 days pass without use, and a year at most. One user keeps at most 20 browsers logged in. A reading app asks for the name and password with the catalog's address.
- `hylly serve --background` keeps the server running hidden, with the settings as its only options: what's typed on `hylly serve`, such as `--lan`, is for one run in that terminal. `hylly server autostart on` starts it at every logon: on Windows it sets up a task; on Linux and macOS it prints the systemd user unit or the launchd agent for this computer, with the commands that install it, and the system then runs the server in the foreground and looks after it.

## 1. One computer

```toml
[server]
# listen = "127.0.0.1"    as it comes: this computer only
```

Nothing to set. The server listens on this computer only, at `http://127.0.0.1:8770/`, so no other device reaches it, and nothing crosses a network.

## 2. A home server

For the tablets, phones and e-readers at home, and your own devices away from home, with HTTPS everywhere through [Tailscale](https://tailscale.com/) Serve. Tailscale is a VPN with a free plan. Once you are the owner (above), step by step:

1. Put Tailscale on the server computer and on every device that can run it, all in the same Tailscale account.
2. At the server computer, turn Serve on: `tailscale serve --bg 8770`, with [Tailscale's command](#tailscales-command) for that system. `tailscale serve status` then says the address, `https://….ts.net`.
3. Put that address in `address` under `[server]` in the settings (`hylly settings edit`), and for a device at home that can't run Tailscale, `listen = "0.0.0.0"` too, as below. Then `hylly server restart`.
4. On each device, open that address and log in, once. In a reading app, add it followed by `opds` (an e-reader: `ereader/opds`), with your name and password.

```toml
[server]
listen = "0.0.0.0"                                # every IPv4 address: for a device at home that can't run Tailscale
address = "https://lantern-pc.tail1234.ts.net"    # the address people use: what tailscale serve status shows
```

```
tailscale serve --bg 8770          # on
tailscale serve status             # on? at which address
tailscale serve --https=443 off    # off
```

- **HTTPS at home too.** Use the `https://….ts.net` address on every device with Tailscale, at home as well. The plain home address (`http://192.0.2.10:8770`, say) carries names, passwords and books across the Wi-Fi as they are. Nobody outside your Tailscale network can reach the Serve address, and no port is opened on your router. Serve runs on this computer, so its forwarding headers count as they come (`trusted_proxies`): the lockout after wrong passwords and the access log see each device's own address.
- **`address`** is what the server's start, `hylly server status`, the Server page and the one-time links show other devices. It is also a name the server answers to, which this computer's name in Tailscale may not be otherwise.
- **A device that can't run Tailscale,** an e-reader with KOReader for example, uses the plain home address with a name and password: `http://192.0.2.10:8770/ereader/opds`, with this computer's own address at home in it. That is what `listen = "0.0.0.0"` is for, and then, with `address` set, `hylly server status`, the Server page and `hylly user link NAME` show that one too, as *Home network*. With Tailscale on every device, leave `listen` as it comes, as Serve reaches the server on this computer. Give such a device a reader account of its own (`hylly user add NAME --role reader`): what could be read off the Wi-Fi is then a login that only reads, and its password can be changed, or the account removed, without touching yours.
- **The firewall: private networks only.** When Windows asks whether Python may accept connections, allow private networks, never public ones.
- **Or without Serve:** with `listen = "0.0.0.0"`, your devices with Tailscale can also use this computer's Tailscale address, `http://100.x.y.z:8770/` (`tailscale ip -4` shows it). They log in there as anywhere; Tailscale encrypts the way, though the address is HTTP.
- `hylly serve --lan` listens on every IPv4 address for that run and its restarts, without the setting.
- **An Inbox for new books.** The library's own share can stay read-only for everyone: give the library an Inbox, a share of its own, and the server takes in what's dropped there, books of every kind, each into the folder of yours its kind belongs in. Under `[inbox]`, its folder, and where each kind goes:

  ```toml
  [inbox]
  folder = '/srv/inbox'
  into = { default = "collections" }   # comics to comics, manuals to manuals, every other book to collections
  ```

  Once a minute the server takes each book that has stayed the same for two minutes, copies it in and checks the copy, asks the lookup sources about it within their budgets, builds the catalog, and only then removes it from the Inbox: a book gone from there is one the library has. What can't be taken goes into the Inbox's `Not added` folder, with a note saying why. With the server walled in ([examples/hylly.service](../examples/hylly.service)), which can see neither the Inbox nor the lookups' keys, the timer below runs it instead, as it does with `jobs = "external"`, and must be installed: its service lets it write the Inbox and read those keys (`BindPaths=-/srv/inbox`, the keys' files alone of the logins folder). The help's *The Inbox* says the rest.

## 3. Open to the internet

For an e-reader or a friend's device away from home, which can't run Tailscale: Tailscale Funnel, or an HTTPS proxy of your own (Caddy, nginx, a domain of yours), pointed at the one port. Hylly is tried most behind Tailscale. Step by step, with Funnel:

1. Give every user a long password (`hylly user add NAME`, at the server computer only), and set up the alarms on your phone (`[bell]`), for wrong passwords and new devices.
2. At the server computer, turn Funnel on: `tailscale funnel --bg 8770`, with [Tailscale's command](#tailscales-command) for that system. `tailscale funnel status` then says the address.
3. Put it in `address` under `[server]` in the settings (Funnel needs no change to `listen`), and run `hylly server restart`.
4. Run `hylly doctor`: it says whether it's all set up safely (below).
5. On the device away from home, open that address and log in. In a reading app, add a catalog at it followed by `opds` (an e-reader: `ereader/opds`), with the name and password: it starts with `https://` and has no port.

A proxy of your own takes Funnel's place in the second step (below), and the address is then yours.

```toml
[server]
listen = "127.0.0.1"                       # as it comes: the tunnel or proxy is on this computer
address = "https://books.example.org"      # or the https://….ts.net address Funnel gives
# trusted_proxies = ["127.0.0.0/8", "::1", "192.0.2.20"]    # only for a proxy on another computer: its address too
```

```
tailscale funnel --bg 8770          # on
tailscale funnel status             # on? at which address
tailscale funnel --https=443 off    # off
```

- **Funnel** gives the same `https://….ts.net` address to the whole internet. **The login page is then on the public internet:** new addresses are listed openly, and scanners try them within minutes. They get only "login needed", and an address with 10 wrong passwords within 15 minutes waits 15 minutes. Turn Funnel off when nobody needs it.
- **With Funnel, as with Serve,** there is nothing more to set: logins through them are what Hylly is tried with most.
- **A proxy of your own** gives HTTPS, points at the server's one port, and hands on three things: who sent the request, in `X-Forwarded-For` (set, or added to); that it came over HTTPS, in `X-Forwarded-Proto`; and the name the browser used, as the `Host` it came with or in `X-Forwarded-Host`. Without that name, the login form's own address (its `Origin`) isn't one of the server's, and the login is refused (403). Caddy does all three as it comes. nginx as it comes does none of them, and sends `Host` as the address it proxies to, so its `location` needs:

  ```
  proxy_pass http://127.0.0.1:8770;
  proxy_set_header Host $host;                                   # $http_host when the address has a port
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  ```

  A proxy on this computer needs nothing more in the settings. One on another computer or in a container needs `listen` to reach it (`"0.0.0.0"`, or this computer's address on that network) and its own address in `trusted_proxies`, as uvicorn's `forwarded_allow_ips`, Jellyfin's known proxies and Home Assistant's `trusted_proxies` do: only those proxies' forwarding headers count, for who sent a request (the lockout, the access log), whether it came over HTTPS, and the name the browser used. Behind cloudflared, `proxy_header = "CF-Connecting-IP"`. A name the proxy calls the server by, other than an address or the name in `address`, goes in `allowed_hosts`.
- **`hylly doctor`** says where the server listens, the address people use and who can log in, and notes an `address` that isn't https, forwarding headers from an address not in `trusted_proxies`, and wrong logins from a proxy that names no sender.
- **On Linux, walled in:** [examples/hylly.service](../examples/hylly.service) runs the server as a system-wide service with most of the computer out of its reach: `hylly serve --service-file` prints it for this computer, filled in from the settings, with the sudo commands that install it, and `hylly doctor` says when the one installed doesn't match the settings (the user, the library, the port). Its `SocketBindAllow` names the one port, and its `SuccessExitStatus=75` and `RestartForceExitStatus=75` let `hylly server restart` and the server page's Restart through. The server writes its own log, `server.log` in the settings folder, and to the journal too (`journalctl -u hylly`).
- **On Windows and macOS** there are no such walls: the server runs with the rights of your whole account, so a flaw in it, in reading a book say, could reach your own files, and the shops' logins and the lookups' keys saved in the `logins` folder of the settings folder. Two ways keep more out of its reach, each its own part. A separate standard account for Hylly, the server and the downloads alike, keeps your own files out of it, but not the logins the downloads use there; and the server's start at logon then runs only while that account is logged in (switching users keeps it so). Keeping the shops' logins on another computer, and giving one only to the download that needs it, in its variable in the environment (`HYLLY_HUMBLE_SESSION` and the others in `settings.example.toml`), keeps the logins out, as none is saved where the server runs. `hylly doctor` says so when an `address` is set and logins are saved there.
- **This computer must be on and awake,** with Tailscale and the library server running. Both start at logon, and Serve and Funnel stay on after a restart.

## 4. In a container

Planned, later.

## Its periodic jobs, on a timer

The server itself makes the nightly copy of the database and the settings, and looks every minute for what the alarms watch. To have a timer run these instead, as a container or a systemd setup may want, set `jobs = "external"` under `[server]`, and start `hylly server jobs` every minute: on Linux with [examples/hylly-jobs.timer](../examples/hylly-jobs.timer) and its [hylly-jobs.service](../examples/hylly-jobs.service), or a line in cron (`* * * * * /home/someone/.local/bin/hylly server jobs`), or a task in Task Scheduler. It runs what's due and ends: the alarms' look itself, and the nightly copy and the Inbox's pass as programs of their own, which it doesn't wait for. `hylly server jobs --dry-run` says which are due. Beside a server walled in it runs the Inbox, and nothing else, even with `jobs` as it comes: the walls keep the Inbox's folders and the lookups' keys from that server. The service has the same walls as the server's own, [examples/hylly.service](../examples/hylly.service), so that what the jobs write is out of a walled server's reach. That the server itself is down it can't say: that's for `OnFailure=` in its systemd service, or a container's health check of `/healthz`.

## Tailscale's command

On Windows, `tailscale.exe` is in `C:\Program Files\Tailscale`. On Linux, `tailscale serve` and `tailscale funnel` need `sudo` before them, unless you once run `sudo tailscale set --operator=$USER`. On macOS, the App Store app's command is `/Applications/Tailscale.app/Contents/MacOS/Tailscale`, which isn't on the PATH. The first time, Tailscale asks you at a link to turn HTTPS on for your account, and for Funnel to allow Funnel too. Both are part of Tailscale's free plan.
