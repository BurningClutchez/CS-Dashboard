# Installing homelab-dash, one step at a time

Every step has **one command** to run and **one check** that proves it worked.
Do not go on to the next step until the check passes. Commands that start with
`sudo` change the system. Everything else only reads.

Where everything goes:

| What | Where | Owner / mode |
|---|---|---|
| Code and web page | `/opt/homelab-dash/` | root, 755/644 (nothing can rewrite it) |
| Config | `/etc/homelab-dash/config.ini` | root, 644 (contains no secrets) |
| Secrets | `/etc/homelab-dash/secrets/*` | root, folder 700, files 600 |
| Data (JSON) | `/var/lib/homelab-dash/` | root, folder 755; `status.json`/`history.json` 644, `state.json` 600 |
| Units | `/etc/systemd/system/homelab-dash-*` and `*.service.d/` drop-ins | root, 644 |
| Firewall | one rule for the LAN and one for Tailscale, on the dashboard port | |

Nothing else is touched. The [Rollback](#rollback-remove-everything) section removes all of it.

---

## 0. Before you start

**0.1 Check Python and systemd.** You need Python 3.8 or newer (standard library only) and systemd 247 or newer.

```bash
python3 --version; systemctl --version | head -1
```
✅ Check: `Python 3.8` or higher, and `systemd 247` or higher. (Ubuntu 22.04+, Debian 12+, Fedora 36+ are fine.)

**0.2 (Optional, recommended) Install smartmontools for drive health.** Version 7.0 or newer is needed for `--json`.

```bash
sudo apt install smartmontools        # Debian/Ubuntu.  Fedora: sudo dnf install smartmontools
```
✅ Check: `smartctl --version | head -1` shows `smartctl 7.x`.

---

## 1. Get the code and test it offline

**1.1 Download the code into your home folder** (as your normal user, not root).

```bash
git clone https://github.com/BurningClutchez/CS-Dashboard.git ~/homelab-dash-src
```
✅ Check: `ls ~/homelab-dash-src` lists `collect.py serve.py detect.py homelab_dash web systemd config tests docs`.

> If the repo is private and `git clone` asks for a password, download the ZIP from
> GitHub on your PC and copy it over with `scp`, then `unzip` it to `~/homelab-dash-src`.
> If the work is not merged yet, add `--branch ccr-ad541958-jj71gd` to the clone command.

**1.2 Run the offline tests.** They use a fake system: fake `/proc`, fake `smartctl`, fake Docker, Pi-hole, Jellyfin, Plex and game servers. They don't touch your real system and don't need root.

```bash
cd ~/homelab-dash-src && python3 -m unittest discover -s tests
```
✅ Check: the last line is `OK` (64 tests; it takes under a minute).

**1.3 (Optional) Look at the page with fake data** before installing anything.

```bash
python3 ~/homelab-dash-src/tests/demo.py --port 8099
```
✅ Check: the server listens only on 127.0.0.1. From your PC run
`ssh -L 8099:127.0.0.1:8099 you@server`, then open <http://127.0.0.1:8099/>.
You should see a dashboard for a made-up host called "homeserver". Press Ctrl+C to stop; the demo deletes its temp folder.

---

## 2. Install the code (root-owned, read-only)

**2.1 Copy the code to /opt.**

```bash
sudo install -d -o root -g root -m 755 /opt/homelab-dash && sudo cp -r ~/homelab-dash-src/{collect.py,serve.py,detect.py,unlock.py,homelab_dash,web,docs} /opt/homelab-dash/
```

**2.2 Make root the owner of every file and make them read-only for everyone else.**

```bash
sudo chown -R root:root /opt/homelab-dash && sudo chmod -R u=rwX,go=rX /opt/homelab-dash
```
✅ Check: `sudo find /opt/homelab-dash ! -user root -o -perm /022 -type f` prints **nothing**.

---

## 3. Let the installer look at your server

**3.1 Create the config folder.**

```bash
sudo install -d -o root -g root -m 755 /etc/homelab-dash
```
✅ Check: `ls -ld /etc/homelab-dash` shows `drwxr-xr-x root root`.

**3.2 Run the detector.** It only reads. It looks at your OS, CPU, RAM, drives, sensors, filesystems,
network, Tailscale, firewall and a free port. It also finds systemd services (Samba, Jellyfin, Pi-hole,
Tailscale, …), Docker containers (grouped by Compose stack, with sidecars, Home Assistant
add-ons and macvlan/ipvlan networks) and LinuxGSM game servers. Then it writes a *draft* config.

```bash
sudo python3 -I -B /opt/homelab-dash/detect.py --write /etc/homelab-dash/config.ini.draft
```
✅ Check: a report prints, ending with `Draft config written to /etc/homelab-dash/config.ini.draft`.
Read the report. If it ends with a `== PROBLEMS` section, fix those first (for example, Python too old or smartctl too old).

Things to look for in the report:
* **LAN subnet**: is it your real home network (for example `192.168.1.0/24`)? If it says *GUESSED*, check it with `ip -4 addr`.
* **Firewall**: `ufw`, `firewalld` or `NONE`. Step 7 uses this.
* **macvlan**: if it says *the host cannot reach these IPs*, read [macvlan](#macvlan-containers-pi-hole-on-its-own-ip) before you enable Pi-hole stats.
* **/home is its own filesystem**: read [Separate /home](#separate-home).

**3.3 Review and edit the draft.** Delete what you don't want, uncomment what you do (remove the `# `),
fix URLs, and set `show_titles` if you want to see what's playing. Every option is explained in
`~/homelab-dash-src/config/config.example.ini`.

```bash
sudoedit /etc/homelab-dash/config.ini.draft
```
✅ Check: `grep -nE 'password|token|key *=' /etc/homelab-dash/config.ini.draft | grep -v _file` prints **nothing**. The config should hold only *paths* to secret files.

**3.4 Make the draft the real config.**

```bash
sudo install -o root -g root -m 644 /etc/homelab-dash/config.ini.draft /etc/homelab-dash/config.ini
```
✅ Check: the config validates and lists what will be checked:
`python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --check-config`
should print `config OK` followed by the list of cards. Any mistake is reported with its section name.

---

## 4. Secrets (only if you enabled Pi-hole, Jellyfin/Emby or Plex stats)

> **Keeping your secrets in an Ansible vault and want no copy on disk?** Skip this step and follow
> [SECRETS-FROM-VAULT.md](SECRETS-FROM-VAULT.md) instead. With that setup the secrets live in RAM only.

The values are typed at a hidden prompt (`read -s`). They never appear on the
command line, in `ps`, in your shell history, in the config, or on the page.
`printf` is a shell builtin, so the secret is not passed as a process argument.

**4.1 Create the root-only secrets folder.**

```bash
sudo install -d -o root -g root -m 700 /etc/homelab-dash/secrets
```
✅ Check: `sudo stat -c '%a %U' /etc/homelab-dash/secrets` prints `700 root`.

**4.2 Pi-hole.** Pi-hole v6: in the web UI go to *Settings → Web interface / API → Advanced → Configure app password*
(recommended: it can be revoked on its own). Pi-hole v5: *Settings → API → Show API token*.

```bash
read -rs -p "Pi-hole password/token: " S; echo; printf '%s\n' "$S" | sudo install -o root -g root -m 600 /dev/stdin /etc/homelab-dash/secrets/pihole.pass; unset S
```
✅ Check: `sudo stat -c '%a %U %s bytes' /etc/homelab-dash/secrets/pihole.pass` prints `600 root` and a size greater than 1.

**4.3 Jellyfin / Emby.** *Dashboard → API Keys → +* and name it `homelab-dash`.

```bash
read -rs -p "Jellyfin API key: " S; echo; printf '%s\n' "$S" | sudo install -o root -g root -m 600 /dev/stdin /etc/homelab-dash/secrets/jellyfin.key; unset S
```
✅ Check: `sudo stat -c '%a %U %s bytes' /etc/homelab-dash/secrets/jellyfin.key` prints `600 root …`.

**4.4 Plex.** You need your account token (Plex support article "Finding an authentication token / X-Plex-Token").

```bash
read -rs -p "Plex token: " S; echo; printf '%s\n' "$S" | sudo install -o root -g root -m 600 /dev/stdin /etc/homelab-dash/secrets/plex.token; unset S
```
✅ Check: `sudo stat -c '%a %U %s bytes' /etc/homelab-dash/secrets/plex.token` prints `600 root …`.

The collector refuses to use a secret file that is group- or world-readable, a symlink, or not owned by root. The card then says so, without showing the value.

---

## 5. Dry run of the collector, by hand

**5.1 Dry run.** This runs every check once, prints the JSON and a list of errors, and **writes nothing**.

```bash
sudo python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --dry-run > /tmp/hldash-dry.json
```
✅ Check: the last lines on screen look like
`# dry run: 9 cards, 0 with errors, 2400 ms, nothing written`. Fix any error listed under it (usually a URL, a key file, or a service name), then run the dry run again.
Also confirm nothing was written: `ls /var/lib/homelab-dash` should say *No such file or directory*.
You can look through the output with `less /tmp/hldash-dry.json`, then delete it: `rm /tmp/hldash-dry.json`.

---

## 6. Install the systemd units

**6.1 Copy the three unit files.**

```bash
sudo install -o root -g root -m 644 ~/homelab-dash-src/systemd/homelab-dash-collector.service ~/homelab-dash-src/systemd/homelab-dash-collector.timer ~/homelab-dash-src/systemd/homelab-dash-web.service /etc/systemd/system/
```
✅ Check: `systemd-analyze verify /etc/systemd/system/homelab-dash-*.service /etc/systemd/system/homelab-dash-*.timer` prints **nothing**.

**6.2 Create the web drop-in folder.**

```bash
sudo install -d -o root -g root -m 755 /etc/systemd/system/homelab-dash-web.service.d
```

**6.3 Write the web drop-in: which networks may connect.** systemd enforces this inside the kernel (cgroup BPF), on top of the firewall. It still protects the page if a firewall rule is ever lost. It also sets `SocketBindAllow=` if you chose a port other than 8088.

```bash
sudo python3 -I -B /opt/homelab-dash/detect.py --web-dropin --port 8088 | sudo install -o root -g root -m 644 /dev/stdin /etc/systemd/system/homelab-dash-web.service.d/10-network.conf
```
✅ Check: `cat /etc/systemd/system/homelab-dash-web.service.d/10-network.conf` shows
`IPAddressAllow=<your LAN subnet> 100.64.0.0/10 fd7a:115c:a1e0::/48`. Use your own port in place of 8088 if you changed it.

**6.4 (Only if the detector mentioned /home or missing smartctl) Write the collector drop-in.**

```bash
sudo install -d -m 755 /etc/systemd/system/homelab-dash-collector.service.d && sudo python3 -I -B /opt/homelab-dash/detect.py --collector-dropin | sudo install -m 644 /dev/stdin /etc/systemd/system/homelab-dash-collector.service.d/10-local.conf
```
✅ Check: `cat /etc/systemd/system/homelab-dash-collector.service.d/10-local.conf`. Each line has a comment explaining it.

**6.5 Tell systemd about the new files.**

```bash
sudo systemctl daemon-reload
```
✅ Check: `systemctl cat homelab-dash-web.service | grep -c IPAddressAllow` prints `2` (the base unit plus your drop-in).

**6.6 First real run, inside the sandbox (the timer is still off).**

```bash
sudo systemctl start homelab-dash-collector.service
```
✅ Check 1: `systemctl status homelab-dash-collector.service --no-pager` shows `Deactivated successfully` and a log line `ok: N cards, 0 check errors …`.
✅ Check 2: `sudo python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --summary` shows the same errors as the dry run (ideally none). **An error that appears here but not in the dry run means the sandbox is blocking something.** See [Troubleshooting](#troubleshooting).
✅ Check 3: `ls -l /var/lib/homelab-dash` shows `status.json` and `history.json` as `-rw-r--r-- root`, and `state.json` as `-rw------- root`.

---

## 7. Start the web server

**7.1 Start the web server now and at every boot.**

```bash
sudo systemctl enable --now homelab-dash-web.service
```
✅ Check 1: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/` prints `200`.
✅ Check 2: it is read-only. `curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8088/status.json` prints `405`, and `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/state.json` prints `404`.
✅ Check 3: it does not run as root. `ps -o user=,cmd= -C python3 | grep serve.py` shows a user like `homelab-dash-web` (a `DynamicUser`), not `root`.

---

## 8. Firewall: home network and Tailscale only

**8.1 Print the exact rules for your firewall.** This only prints them; it changes nothing.

```bash
sudo python3 -I -B /opt/homelab-dash/detect.py --firewall --port 8088
```
Then run each printed line, one at a time. With **ufw** they look like this:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8088 proto tcp comment 'homelab-dash LAN'
```
```bash
sudo ufw allow in on tailscale0 to any port 8088 proto tcp comment 'homelab-dash tailscale'
```
✅ Check: `sudo ufw status | grep 8088` shows exactly those two rules. There must be no `8088 ALLOW Anywhere`.

With **firewalld**, the detector prints `firewall-cmd --permanent --add-rich-rule=…` lines followed by `sudo firewall-cmd --reload`.
✅ Check: `sudo firewall-cmd --list-rich-rules | grep 8088` shows them.

**If the detector says no firewall is active:** the systemd `IPAddressAllow=` from step 6.3 already limits the page to your LAN and Tailscale. If you also want ufw, **allow SSH first**, ideally while you have a keyboard and screen on the server:
`sudo ufw allow OpenSSH`, then `sudo ufw enable`, then run the two rules above.
(Docker-published ports skip ufw. That doesn't affect this dashboard, because it doesn't run in Docker.)

✅ Final check, from your phone:
* On home Wi-Fi: `http://<server LAN IP>:8088/` loads.
* On mobile data with Tailscale **on**: `http://<tailscale IP or MagicDNS name>:8088/` loads.
* On mobile data with Tailscale **off**: it does not load (it times out).

If a name gives `421 unknown host name`, add that name to `allowed_hosts` in `[web]` and run `sudo systemctl restart homelab-dash-web`.
This check protects you against DNS rebinding.

---

## 9. Turn on the timer

**9.1 Start the timer now and at every boot.**

```bash
sudo systemctl enable --now homelab-dash-collector.timer
```
✅ Check 1: `systemctl list-timers homelab-dash-collector.timer --no-pager` shows the next run less than a minute away.
✅ Check 2: two minutes later, `sudo python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --summary` says `written NN s ago`, with NN under 90.

---

## 10. Add it to your phone's home screen

* **iPhone (Safari):** open the page, tap Share, then *Add to Home Screen*. It opens full-screen like an app.
* **Android (Chrome):** open the page, tap ⋮, then *Add to Home screen*. Over plain `http://`, Chrome adds a shortcut that opens in the browser. Full "install as app" needs HTTPS, which this read-only page doesn't need.

The page refreshes every 30 seconds. It also refreshes the moment you switch back to it. It pauses polling while it's hidden, to save battery. If the data is more than 3 minutes old it says **Data is stale**. If a request fails or takes more than 10 seconds it shows **Can't load data** and dims the last good data.

---

## 11. Security score of the units

**11.1 Score both units.**

```bash
systemd-analyze security homelab-dash-collector.service homelab-dash-web.service --no-pager | grep -E 'Overall|→'
```
✅ Check: about **2.4 OK** for the collector and about **1.0** for the web server (0.9 SAFE without the LAN allow-list, 1.0 with it). Lower is better; 10 is fully exposed. Typical system services score 5–9.6.

Why the collector isn't lower, and why each remaining item is there:

| Item it still flags | Why it's needed | What limits it |
|---|---|---|
| Runs as root | SMART, the Docker socket and the journal need root | Capabilities are cut down to `CAP_SYS_RAWIO` and `CAP_SYS_ADMIN` only; the whole filesystem is read-only except `/var/lib/homelab-dash`; `/root`, `/home`, `/etc/ssh` and `/etc/shadow` are hidden |
| `CAP_SYS_RAWIO`, `CAP_SYS_ADMIN` | `smartctl` needs these for ATA passthrough and NVMe log pages | Only kept if smartctl is installed (otherwise the drop-in from step 6.4 removes them) |
| Device access | reading SMART | `DevicePolicy=closed` + read-only access to disks (`block-sd`, NVMe) and ZFS control only |
| Network sockets | HTTP checks, Docker/Tailscale sockets | `IPAddressAllow` = localhost + private ranges only: **no Internet access** |
| Full `/proc` | process checks (LinuxGSM) and CPU/RAM | read-only |

The web server runs as a throw-away `DynamicUser` with **no** capabilities, private `/dev`, `/tmp` and user namespace.
It sees only `/opt/homelab-dash` and `/var/lib/homelab-dash`; the secrets folder is made inaccessible.
It can bind only TCP 8088, and only your LAN, Tailscale and localhost can talk to it.

**About the Docker socket:** anything that can write to `/run/docker.sock` is effectively root.
The collector only ever sends HTTP `GET` requests (`/containers/json`, `/networks`,
`/containers/<name>/json`). The offline tests include a fake Docker that counts any write attempt and fails the test if there is one.

---

## Special cases

### macvlan containers (Pi-hole on its own IP)

By design in Linux, a host cannot talk to its own macvlan containers. The Docker card still
shows their state (from the Docker API), but Pi-hole/Jellyfin **stats** need a URL the host can reach. Pick one:

1. **Easiest:** also publish the web port on localhost. In that service's compose file, add
   `ports: ["127.0.0.1:8081:80"]` and a normal bridge network next to the macvlan one, then
   `docker compose up -d`. Set `url = http://127.0.0.1:8081` in `[pihole]`.
2. **macvlan shim:** add a host-side macvlan interface with a spare IP and a route to the container.
   This is a network change outside this project; only do it if you already know you want it.

The detector marks such a URL *NOT reachable from the host* and leaves the section commented out until you fix it.

### Separate /home

If `/home` is its own filesystem, `ProtectHome=yes` hides it, and the disk card would show
*hidden by ProtectHome*. The drop-in from step 6.4 switches the collector to
`ProtectHome=read-only`, which still can't write anything there.

### Changing the port

Change `port` in `[web]`. Re-run step 6.3 with `--port NEW` (that adds `SocketBindAllow=tcp:NEW`). Then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart homelab-dash-web
```

Then update the firewall rules (step 8; delete the old ones as shown in Rollback).

### Updating the code later

```bash
cd ~/homelab-dash-src && git pull && python3 -m unittest discover -s tests
```

When that prints `OK`, repeat steps 2.1–2.2. Then:

```bash
sudo systemctl restart homelab-dash-web
```

---

## Troubleshooting

| Symptom | Look at | Usual cause |
|---|---|---|
| Page says *Can't load data* | `systemctl status homelab-dash-web` | web server stopped; firewall; wrong IP/port |
| Page says *Data is stale* | `systemctl list-timers homelab-dash-collector.timer`, `journalctl -u homelab-dash-collector -n 50` | timer off; collector failing (config error exits with code 2) |
| `421 unknown host name` | `[web] allowed_hosts` | add the name you type in the browser |
| An error only in the sandbox (step 6.6), not in the dry run | `journalctl -u homelab-dash-collector -n 50` | a path hidden by the sandbox (for example `/home`, see above), or an address outside `IPAddressAllow` (a public IP). Add an exact drop-in line rather than removing protections |
| SMART "permission denied" | `sudo smartctl -a /dev/sdX` outside the sandbox | USB bridge needs `-d sat` → `smart = /dev/sdX:sat` |
| Pi-hole "HTTP 429" | Pi-hole sessions | too many logins from elsewhere; the collector re-uses one session, so wait 5 minutes |
| Pi-hole / Jellyfin "unreachable" | URL in config | macvlan (see above) or wrong port |

---

## Rollback: remove everything

Run these in order. Each step undoes one install step. At the end nothing from this project is left.

**R1. Stop and disable the timer and both services.**

```bash
sudo systemctl disable --now homelab-dash-collector.timer homelab-dash-web.service homelab-dash-collector.service
```
✅ Check: `systemctl is-active homelab-dash-web homelab-dash-collector.timer` prints `inactive` twice.

**R2. Remove the unit files and drop-ins.**

```bash
sudo rm -rf /etc/systemd/system/homelab-dash-collector.service /etc/systemd/system/homelab-dash-collector.timer /etc/systemd/system/homelab-dash-web.service /etc/systemd/system/homelab-dash-web.service.d /etc/systemd/system/homelab-dash-collector.service.d
```

**R3. Make systemd forget them.**

```bash
sudo systemctl daemon-reload && sudo systemctl reset-failed
```
✅ Check: `systemctl list-unit-files 'homelab-dash*' --no-legend | wc -l` prints `0`.

**R4. Remove the firewall rules.** ufw: delete the same rules you added (same text, with `delete` in front).

```bash
sudo ufw delete allow from 192.168.1.0/24 to any port 8088 proto tcp
```
```bash
sudo ufw delete allow in on tailscale0 to any port 8088 proto tcp
```
firewalld: run the same `--add-rich-rule` lines with `--remove-rich-rule`, then `sudo firewall-cmd --reload`.
✅ Check: `sudo ufw status | grep 8088` (or `sudo firewall-cmd --list-rich-rules | grep 8088`) prints **nothing**.

**R5. Delete the data, the config and the secrets.** `shred` overwrites the secret files before they are removed.

```bash
sudo find /etc/homelab-dash/secrets -type f -exec shred -u {} + ; sudo rm -rf /etc/homelab-dash /var/lib/homelab-dash /var/lib/private/homelab-dash
```
✅ Check: `ls /etc/homelab-dash /var/lib/homelab-dash` says *No such file or directory* twice.

**R6. Delete the code.**

```bash
sudo rm -rf /opt/homelab-dash
```
✅ Check: `ls /opt/homelab-dash` says *No such file or directory*.

**R7. Revoke the credentials** in the apps themselves:
* Pi-hole: clear the app password.
* Jellyfin/Emby: Dashboard → API Keys → delete `homelab-dash`.
* Plex: sign out of the device the token belonged to (or change your password).

**R8. Optional: remove the downloaded source**, and smartmontools only if you installed it just for this.

```bash
rm -rf ~/homelab-dash-src
```

Only if you installed smartmontools for this dashboard (it is also useful on its own):

```bash
sudo apt remove smartmontools
```

✅ Final check: `sudo ss -tlnp | grep ':8088 '` prints **nothing**, and `systemctl list-timers --all | grep homelab` prints **nothing**.

The web server's `DynamicUser` leaves no account behind (`getent passwd | grep homelab` prints nothing).
