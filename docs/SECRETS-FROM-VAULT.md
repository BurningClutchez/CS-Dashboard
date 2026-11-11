# Secrets from an Ansible vault, kept in RAM only

Use this instead of [INSTALL.md step 4](INSTALL.md#4-secrets-only-if-you-enabled-pi-hole-jellyfinemby-or-plex-stats)
if your Pi-hole / Jellyfin / Plex secrets live in an **Ansible vault** (in any folder)
and you don't want a copy saved anywhere else.

No playbook is needed. The only Ansible piece used is the `ansible-vault` command, to decrypt.

## How it works

```
your vault (encrypted, anywhere)
   │  ansible-vault view   (decrypts in memory, prompts for the vault password)
   ▼  pipe
unlock.py  ──writes only the mapped keys──▶  /run/homelab-dash-secrets/   (tmpfs = RAM)
                                              root:root 700, files 600
                                                     │ read every minute
                                                     ▼
                                              collector (sandboxed)
```

* `/run` is a **tmpfs**: it lives in RAM and is wiped at every reboot.
  `unlock.py` **refuses** to write anywhere that isn't tmpfs/ramfs.
* The decrypted text only passes through a pipe. `ansible-vault view` doesn't write temp files,
  and nothing goes into your shell history or the process list.
* Only the keys you map are written. Every other key in your vault is ignored, and
  `unlock.py` never prints a value, only key and file names.
* The Pi-hole login session is also kept in the RAM folder (`session_file`), not in
  `/var/lib/homelab-dash/state.json` on disk.
* The web server can't see the folder at all (`InaccessiblePaths=`).

**The trade-off:** after every reboot the Pi-hole / Jellyfin / Plex cards say
*"secrets are locked"* until you run the unlock command again. Everything else on the
dashboard works without secrets.

## One-time setup

**V1. Point the config at the RAM folder.** Edit `/etc/homelab-dash/config.ini`:

```bash
sudoedit /etc/homelab-dash/config.ini
```

Add a `[vault]` section that maps **your vault's key names** to file names. Then point
the `*_file` options at those files:

```ini
[vault]
dir = /run/homelab-dash-secrets
map = pihole_password=pihole.pass jellyfin_api_key=jellyfin.key plex_token=plex.token

[pihole]
url = http://127.0.0.1:8081
password_file = /run/homelab-dash-secrets/pihole.pass
session_file  = /run/homelab-dash-secrets/pihole.sid

[media:jellyfin]
type = jellyfin
url = http://127.0.0.1:8096
key_file = /run/homelab-dash-secrets/jellyfin.key
```

If your vault calls it `vault_pihole_webpassword`, write `map = vault_pihole_webpassword=pihole.pass`.

✅ Check: `python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --check-config` prints `config OK`.

**V2. Install the updated units** (they create the RAM folder and hide it from the web server). Then:

```bash
sudo install -o root -g root -m 644 ~/homelab-dash-src/systemd/homelab-dash-*.service /etc/systemd/system/ && sudo systemctl daemon-reload
```
✅ Check: `systemctl cat homelab-dash-collector.service | grep RuntimeDirectory=` prints `RuntimeDirectory=homelab-dash-secrets`.

**V3. If you used INSTALL step 4 before, wipe the old copies on disk.**

```bash
sudo find /etc/homelab-dash/secrets -type f -exec shred -u {} + ; sudo rmdir /etc/homelab-dash/secrets
```
✅ Check: `ls /etc/homelab-dash/secrets` says *No such file or directory*.

## Unlock (after setup and after every reboot)

Your vault's values must be single-line `key: value` entries (plain, `"double"` or
`'single'` quoted). That's the normal layout for passwords and tokens.

**U1. Get sudo ready first**, so its password prompt doesn't collide with the vault prompt.

```bash
sudo -v
```

**U2. Decrypt and load into RAM.** Use your vault's real path. Add `--vault-password-file …` if you use one.

```bash
ansible-vault view ~/ansible/vault/homelab.yml | sudo python3 -I -B /opt/homelab-dash/unlock.py
```
✅ Check: it prints `loaded into RAM (/run/homelab-dash-secrets): pihole_password -> pihole.pass, …`.
If it prints a **swap warning**, read [Swap](#swap) below.

**U3. Confirm the cards picked it up** (within a minute):

```bash
sudo python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini --summary
```
✅ Check: no `secrets are locked` errors on the Pi-hole and media cards.

**Vault on another machine (for example your laptop)?** Pipe it over SSH. Sudo can't prompt in the
middle of a pipe, so allow exactly this one command without a password on the server
(`sudo visudo -f /etc/sudoers.d/homelab-dash-unlock`):

```
yourname ALL=(root) NOPASSWD: /usr/bin/python3 -I -B /opt/homelab-dash/unlock.py
```

Then, from the laptop:

```bash
ansible-vault view ~/ansible/vault/homelab.yml | ssh yourname@homeserver sudo -n /usr/bin/python3 -I -B /opt/homelab-dash/unlock.py
```

The secret travels only inside the SSH connection and lands only in the server's RAM.

## Other commands

* See which secrets are loaded (names only):

  ```bash
  sudo python3 -I -B /opt/homelab-dash/unlock.py --status
  ```

* Wipe them from RAM now (for example before lending the server out):

  ```bash
  sudo python3 -I -B /opt/homelab-dash/unlock.py --lock
  ```

* Rotate a key: change it in the vault, then run U1 and U2 again. Nothing needs a restart.

## Swap

tmpfs pages can be written to swap when RAM is short. `unlock.py` warns if swap is on
a disk (`/swap.img`, a partition) rather than zram. To be strict, pick one:

* Use zram swap only (stays in RAM). On Ubuntu/Debian: `sudo apt install systemd-zram-generator`, then turn off the disk swap.
* Encrypt the disk swap.
* Run without swap.

## Reboot test (add to TEST-PLAN stage 4)

| # | Test | Pass if |
|---|---|---|
| 4.12 | After reboot, before unlocking | Pi-hole / media cards show *secrets are locked*; the LAN addresses card notes "Pi-hole data unavailable" for its domains; every other card is OK |
| 4.13 | `ls /run/homelab-dash-secrets` | empty (or only appears after the first collector run) |
| 4.14 | Run U1 and U2, wait 1 minute | those cards are back |
| 4.15 | No copy on disk: `sudo grep -rl "pihole_sid" /var/lib/homelab-dash/` | prints nothing |

## Rollback

```bash
sudo python3 -I -B /opt/homelab-dash/unlock.py --lock
```

Then remove the `[vault]` section and the `/run/…` paths from the config. Also delete the
sudoers file if you made one: `sudo rm /etc/sudoers.d/homelab-dash-unlock`. The main
[Rollback](INSTALL.md#rollback-remove-everything) removes everything else. A reboot also clears the RAM folder.
