# Test plan

Four stages: offline (before touching the server), at install time, a 24-hour
soak, and a reboot. Write down pass/fail for each line. If anything fails, stop
and check [Troubleshooting](INSTALL.md#troubleshooting) before you continue.

Shorthand used below:

```bash
SUMMARY='sudo python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --summary'
```

## Stage 1: Offline, on fake data (no root, nothing installed)

| # | Test | Command | Pass if |
|---|---|---|---|
| 1.1 | Unit and integration tests | `python3 -m unittest discover -s tests -v` | last line `OK` |
| 1.2 | Demo page renders | `python3 tests/demo.py --port 8099`, open via SSH tunnel | cards appear; Disks shows a degraded md0 and a sleeping drive; Docker groups stacks with sidecars/add-ons/macvlan; 24-hour charts show a gap where the "reboot" was |
| 1.3 | Phone layout | open the demo on the phone (tunnel or same PC) | no sideways scrolling; cards in one column |

What the automated tests cover (tests/test_collector.py, tests/test_webserver_config.py):

* Every card type against a fake `/proc` and `/sys`, fake `smartctl`/`systemctl`/`journalctl`/`ip`/`tailscale`, and a fake Docker API, plus fake Pi-hole v5/v6, Jellyfin, Plex, Steam-query and Minecraft servers.
* **Soft failure:** a `smartctl` that hangs, an HTTP service that never answers, an offline game server and a card that crashes each show an error on their own card only, and the run still finishes in seconds.
* **Secrets:** the password, keys and token never appear in `status.json` or `history.json`. A secret file that is group/world-readable or a symlink is refused, and the error message doesn't contain the value.
* **Read-only:** the fake Docker counts any write request and must see none. The web server returns 405 for every method except GET/HEAD and 404 for `state.json`, `..` paths and directories. It sends 421 for unknown Host names (DNS rebinding) and `no-store` on JSON.
* Pi-hole re-uses one session instead of logging in every minute.
* `--dry-run` writes nothing. File modes are 644 for status/history and 600 for state.
* The draft config that `detect.py` writes always passes validation.

## Stage 2: Install time

These are the checks in INSTALL.md. The important ones:

| # | Test | Pass if |
|---|---|---|
| 2.1 | Detector report (step 3.2) | OS, CPU, RAM, drives, subnet, firewall, services and containers match what you know is there |
| 2.2 | Dry run (step 5.1) | `0 with errors`, or only errors you understand; `/var/lib/homelab-dash` still does not exist |
| 2.3 | Sandboxed run (step 6.6) | `--summary` shows **the same** errors as the dry run, so the sandbox blocks nothing that's needed |
| 2.4 | Read-only web (step 7.1) | `200` for `/`, `405` for POST, `404` for `/state.json`; process user is not root |
| 2.5 | Firewall (step 8) | loads on Wi-Fi and on Tailscale; **does not** load on mobile data without Tailscale |
| 2.6 | Security score (step 11) | collector ≤ 2.5, web ≤ 1.2 |
| 2.7 | "Can't load" banner | `sudo systemctl stop homelab-dash-web`; on the phone, switch to another app and back: a red **Can't load data** banner appears within about 10 s. Then `sudo systemctl start homelab-dash-web`, switch away and back: the banner goes away |
| 2.8 | "Data is stale" banner | `sudo systemctl stop homelab-dash-collector.timer`; wait 4 minutes: a yellow **Data is stale** banner appears. Then `sudo systemctl start homelab-dash-collector.timer`; within about 90 s it goes away |
| 2.9 | A broken check stays on its card | in the config, set the URL of one `type = http` service to an unused port (for example `http://127.0.0.1:1/`). Within about 60 s only that row turns red, with "connection refused". Put the URL back afterwards |

## Stage 3: 24-hour soak

Start it right after step 9 and note the time. The next day, run each command:

| # | What | Command | Pass if |
|---|---|---|---|
| 3.1 | Collector ran about every minute | `journalctl -u homelab-dash-collector --since -24h -o cat \| grep -c '^ok:'` | about 1,400–1,440 |
| 3.2 | No failed runs | `journalctl -u homelab-dash-collector --since -24h -p warning -o cat \| grep -v '^ok:' \| head` | prints nothing (or only things you understand) |
| 3.3 | Runs are short | `journalctl -u homelab-dash-collector --since -24h -o cat \| grep -o '[0-9]* ms' \| sort -n \| tail -1` | the longest run is well under 30,000 ms. The first SMART read each half hour is the slow one |
| 3.4 | No timeouts or kills | `journalctl -u homelab-dash-collector --since -24h \| grep -ciE 'timeout\|killed'` | `0` |
| 3.5 | History is full and bounded | `sudo python3 -c "import json;h=json.load(open('/var/lib/homelab-dash/history.json'));s=h['samples'];print(len(s),(s[-1][0]-s[0][0])/3600)"` | about `1440` samples covering about `24` hours (it never grows past 24 h) |
| 3.6 | Disk use is tiny | `sudo du -sh /var/lib/homelab-dash` | under 1 MB |
| 3.7 | Web server is healthy and small | `systemctl status homelab-dash-web --no-pager \| grep -E 'Active\|Memory'` | `active (running)` since install; Memory under about 40 MB |
| 3.8 | Web server never restarted | `systemctl show homelab-dash-web -p NRestarts` | `NRestarts=0` |
| 3.9 | HDDs still sleep (if you use spindown) | `sudo smartctl -n standby -i /dev/sdX \| tail -1` while idle | "Device is in STANDBY mode": the dashboard didn't wake it |
| 3.10 | Page over 24 h | open the History section on the phone | charts cover the whole day and match what you'd expect (backups, Plex nights) |
| 3.11 | Data is fresh | `$SUMMARY` | `written NN s ago` with NN < 90, no `STALE` |

## Stage 4: Reboot test

**Before**, record the current state:

```bash
systemctl is-enabled homelab-dash-web homelab-dash-collector.timer; sudo ufw status | grep 8088
```

**Reboot**, ideally while you can reach the console:

```bash
sudo systemctl reboot
```

**After**, wait about 2 minutes after the server is back up, then:

| # | Test | Command | Pass if |
|---|---|---|---|
| 4.1 | Web server came back | `systemctl is-active homelab-dash-web` | `active` |
| 4.2 | Timer came back | `systemctl list-timers homelab-dash-collector.timer --no-pager` | next run under 60 s away |
| 4.3 | First run after boot worked | `journalctl -b -u homelab-dash-collector -o cat \| head -3` | an `ok:` line about 45 s after boot |
| 4.4 | Data fresh | `$SUMMARY` | not `STALE`; no new errors (a container that's still starting may briefly show "starting") |
| 4.5 | Network limits still applied | `systemctl show homelab-dash-web -p IPAddressAllow` | your LAN subnet and the Tailscale ranges |
| 4.6 | Firewall still applied | `sudo ufw status \| grep 8088` (or `firewall-cmd --list-rich-rules`) | same two rules as before |
| 4.7 | Phone | open the home-screen app on Wi-Fi and on Tailscale | loads; "updated … s ago" is under a minute |
| 4.8 | Uptime reset | System card | uptime shows minutes |
| 4.9 | Gap visible | History charts | a short break in the lines at the reboot time (no fake line drawn across it) |

All stages passed: the dashboard is done. To remove it, follow [Rollback](INSTALL.md#rollback-remove-everything).
