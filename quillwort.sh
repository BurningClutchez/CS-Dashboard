#!/usr/bin/env bash
# quillwort.sh: interactive quick installer for homelab-dash.
#
#   sudo bash ./quillwort.sh            # install, asking for paths and settings
#   sudo bash ./quillwort.sh --preview  # walk through everything, change NOTHING
#
# It does the same steps as docs/INSTALL.md, in the same order, with the same
# checks: offline tests, detection, config review, secrets, dry run, units,
# sandboxed run, web server, firewall, timer, security score.
# To undo everything: docs/INSTALL.md, section "Rollback".

set -Eeuo pipefail
umask 022

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREVIEW=0
[[ ${1:-} == "--preview" ]] && PREVIEW=1
[[ ${1:-} == "-h" || ${1:-} == "--help" ]] && { sed -n '2,11p' "$0"; exit 0; }
PY=/usr/bin/python3
STEP="start"
RUN_AS="${SUDO_USER:-root}"
RAM_DIR=/run/homelab-dash-secrets
DATA_DIR=/var/lib/homelab-dash   # fixed: comes from StateDirectory= in the unit

# ------------------------------------------------------------------ helpers
bold=$'\033[1m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; off=$'\033[0m'
[[ -t 1 ]] || { bold=; green=; yellow=; red=; off=; }
step() { STEP="$*"; printf '\n%s== %s%s\n' "$bold" "$*" "$off"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$*"; }
die()  { printf '  %s✗ %s%s\n' "$red" "$*" "$off" >&2; exit 1; }
trap 'printf "\n%s✗ stopped during: %s%s\n  Steps before this one were completed. To undo: docs/INSTALL.md \"Rollback\".\n" "$red" "$STEP" "$off" >&2' ERR

# run CMD...: execute, or only show it in --preview mode
run() {
  if ((PREVIEW)); then printf '  [preview] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
# put FILE MODE: write stdin to FILE (root-owned), or show it in --preview mode
put() {
  if ((PREVIEW)); then printf '  [preview] would write %s (mode %s):\n' "$1" "$2"; sed 's/^/      | /'; else
    install -o root -g root -m "$2" /dev/stdin "$1"; fi
}
ask() {  # ask "Question" "default" -> answer on stdout (prompt goes to the terminal)
  local ans
  read -r -p "  $1${2:+ [$2]}: " ans </dev/tty
  printf '%s' "${ans:-${2:-}}"
}
yes_no() {  # yes_no "Question" y|n
  local a
  a="$(ask "$1 (y/n)" "${2:-n}")"
  [[ $a =~ ^[Yy] ]]
}
safe_dir() {  # reject locations the sandboxes hide or wipe
  [[ $1 == /* ]] || die "$1: use an absolute path"
  case "$1" in
    /home*|/root*|/tmp*|/var/tmp*|/run*|/proc*|/sys*|/dev*) die "$1: not allowed (hidden by the sandbox or wiped at boot)";;
  esac
}

# --------------------------------------------------------------- preflight
step "Preflight"
((PREVIEW)) && warn "PREVIEW mode: nothing on this system will be changed"
if ((! PREVIEW)) && [[ $EUID -ne 0 ]]; then die "run with sudo:  sudo bash $0"; fi
[[ -x $PY ]] || die "$PY not found"
$PY -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "Python 3.8+ needed ($($PY -V 2>&1))"
ok "$($PY -V 2>&1)"
SYSTEMD_VER="$(systemctl --version 2>/dev/null | awk 'NR==1{print $2}')"
if [[ -z $SYSTEMD_VER ]]; then
  ((PREVIEW)) && warn "systemd not found (fine for a preview)" || die "systemd not found"
elif ((SYSTEMD_VER < 247)); then
  warn "systemd $SYSTEMD_VER is older than 247: some sandbox options will be ignored"
else
  ok "systemd $SYSTEMD_VER"
fi
for f in collect.py serve.py detect.py unlock.py homelab_dash web systemd; do
  [[ -e $SRC/$f ]] || die "$SRC/$f missing: run this from the downloaded repo"
done
ok "source: $SRC"

step "Offline tests (fake data, nothing touched)"
if [[ $RUN_AS != root ]]; then
  sudo -u "$RUN_AS" bash -c "cd '$SRC' && $PY -B -m unittest discover -s tests -q" </dev/null 2>&1 | grep -E '^(Ran|OK|FAILED)'
else
  (cd "$SRC" && $PY -B -m unittest discover -s tests -q) 2>&1 | grep -E '^(Ran|OK|FAILED)'
fi
ok "tests passed"

# ------------------------------------------------------------- the questions
step "Detecting this server (read-only)"
FACTS="$($PY -I -B "$SRC/detect.py" --json 2>/dev/null)"
eval "$($PY -c '
import json, shlex, sys
f = json.loads(sys.stdin.read())
n = f.get("network") or {}
ts = n.get("tailscale") or {}
out = {
  "D_SUBNET": n.get("subnet") or "", "D_IP": n.get("ip") or "",
  "D_PORT": f.get("port") or 8088,
  "D_FW": (f.get("firewall") or {}).get("active") or "none",
  "D_TS": "y" if ts else "n", "D_TSNAME": ts.get("dns_name") or "",
  "D_HOME": "1" if f.get("home_separate") else "0",
  "D_SMART": "1" if (f.get("drives") or {}).get("smartctl") else "0",
  "D_HOST": (f.get("host") or {}).get("hostname") or "server",
}
for k, v in out.items():
    print("%s=%s" % (k, shlex.quote(str(v))))
' <<<"$FACTS")"
ok "host $D_HOST, address ${D_IP:-?}, LAN ${D_SUBNET:-?}, firewall $D_FW, Tailscale $([[ $D_TS == y ]] && echo yes || echo no)"
[[ $D_SMART == 1 ]] || warn "smartctl not installed: no drive health (install smartmontools and re-run to add it)"

step "Your choices"
INSTALL_DIR="$(ask "Install the code to" /opt/homelab-dash)";   safe_dir "$INSTALL_DIR"
CONF_DIR="$(ask "Config folder" /etc/homelab-dash)";             safe_dir "$CONF_DIR"
printf '  Data folder is fixed: %s\n' "$DATA_DIR"
PORT="$(ask "Dashboard port" "$D_PORT")"
[[ $PORT =~ ^[0-9]+$ ]] && ((PORT >= 1024 && PORT <= 65535)) || die "port must be 1024-65535"
if ! $PY -c 'import socket,sys; s=socket.socket(); s.bind(("0.0.0.0", int(sys.argv[1])))' "$PORT" 2>/dev/null; then
  [[ -f $CONF_DIR/config.ini ]] && grep -qE "^port *= *$PORT" "$CONF_DIR/config.ini" \
    && warn "port $PORT is in use (by an earlier install, it seems)" \
    || die "port $PORT is already in use; pick another"
fi
SUBNET="$(ask "Home network allowed to open the page" "$D_SUBNET")"
$PY -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$SUBNET" 2>/dev/null \
  || die "'$SUBNET' is not a network like 192.168.1.0/24"
SUBNET="$($PY -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))' "$SUBNET")"
if yes_no "Also allow access over Tailscale" "$D_TS"; then TS=1; else TS=0; fi

echo
printf '  %-22s %s\n' "Code:" "$INSTALL_DIR" "Config:" "$CONF_DIR" "Data:" "$DATA_DIR" \
  "Port:" "$PORT" "Allowed:" "$SUBNET$([[ $TS == 1 ]] && echo ' + Tailscale')"
yes_no "Continue with these settings" y || die "cancelled; nothing was changed"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
CFG_LIVE="$CONF_DIR/config.ini"
CFG="$CFG_LIVE"; ((PREVIEW)) && CFG="$WORK/config.ini"
CODE="$INSTALL_DIR"; ((PREVIEW)) && CODE="$SRC"   # preview runs the code from the repo

# ------------------------------------------------------------------ install
step "Install the code (root-owned, read-only)"
if [[ -e $INSTALL_DIR && ! -f $INSTALL_DIR/collect.py && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
  die "$INSTALL_DIR exists and is not a homelab-dash install; pick another folder"
fi
[[ -f $INSTALL_DIR/collect.py ]] && warn "updating the existing install in $INSTALL_DIR"
run install -d -o root -g root -m 755 "$INSTALL_DIR"
run cp -r "$SRC/collect.py" "$SRC/serve.py" "$SRC/detect.py" "$SRC/unlock.py" "$SRC/homelab_dash" "$SRC/web" "$SRC/docs" "$INSTALL_DIR/"
run find "$INSTALL_DIR" -name __pycache__ -prune -exec rm -rf {} +
run chown -R root:root "$INSTALL_DIR"
run chmod -R u=rwX,go=rX "$INSTALL_DIR"
if ((! PREVIEW)); then
  [[ -z "$(find "$INSTALL_DIR" ! -user root -o -perm /022 -type f)" ]] || die "permission check failed in $INSTALL_DIR"
fi
ok "code in $INSTALL_DIR"

step "Config"
run install -d -o root -g root -m 755 "$CONF_DIR"
NEW_CONFIG=1
if [[ -f $CFG_LIVE ]]; then
  if yes_no "Keep the existing $CFG_LIVE" y; then
    NEW_CONFIG=0; ((PREVIEW)) && cp "$CFG_LIVE" "$CFG"
  else
    run cp -p "$CFG_LIVE" "$CFG_LIVE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
fi
if ((NEW_CONFIG)); then
  DRAFT="$WORK/config.ini.draft"
  $PY -I -B "$SRC/detect.py" --port "$PORT" --write "$DRAFT" >/dev/null || warn "detector reported problems (run detect.py to see them)"
  [[ -f $DRAFT ]] || die "detector did not write a draft"
  [[ $INSTALL_DIR != /opt/homelab-dash ]] && sed -i "/^\[web\]/a web_root = $INSTALL_DIR/web" "$DRAFT"
  [[ $CONF_DIR != /etc/homelab-dash ]] && sed -i "s#/etc/homelab-dash/secrets/#$CONF_DIR/secrets/#g" "$DRAFT"
  ok "draft written from what was detected"
  ED="${EDITOR:-$(command -v nano || command -v vi || echo vi)}"
  while :; do
    if yes_no "Open the draft in $ED to review it (recommended)" y; then
      $ED "$DRAFT" </dev/tty >/dev/tty
    fi
    if grep -E '^[^#]*(password|token|api_?key) *=' "$DRAFT" | grep -vE '_file *=|^ *map *=' >/dev/null; then
      warn "the config must not contain secret values, only *_file paths"; continue
    fi
    if $PY -I -B "$SRC/collect.py" --config "$DRAFT" --check-config; then break; fi
    yes_no "Fix it in the editor" y || die "config not valid; nothing installed beyond the code"
  done
  if ((PREVIEW)); then cp "$DRAFT" "$CFG"; ok "(preview) config kept in a temp folder"
  else install -o root -g root -m 644 "$DRAFT" "$CFG_LIVE"; ok "$CFG_LIVE"; fi
fi

# ------------------------------------------------------------------ secrets
step "Secrets"
mapfile -t SECRETS < <($PY - "$CFG" <<'EOF'
import configparser, sys
cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
cp.read(sys.argv[1])
for s in cp.sections():
    for k in ("password_file", "key_file"):
        v = cp.get(s, k, fallback="").strip()
        if v:
            print("%s|%s" % (s, v))
EOF
)
if ((${#SECRETS[@]} == 0)); then
  ok "no enabled card needs a secret"
else
  echo "  Cards that need a secret:"
  for line in "${SECRETS[@]}"; do printf '    [%s]  %s\n' "${line%%|*}" "${line#*|}"; done
  echo "  How do you want to provide them?"
  echo "    1) type them now (stored in root-only files, mode 600)"
  echo "    2) from an Ansible vault, kept in RAM only (unlock again after each reboot)"
  echo "    3) skip for now (those cards show an error until you add them)"
  MODE="$(ask "Choice" 1)"
  case "$MODE" in
  1)
    SDIR="$CONF_DIR/secrets"
    run install -d -o root -g root -m 700 "$SDIR"
    for line in "${SECRETS[@]}"; do
      sec="${line%%|*}"; path="${line#*|}"
      [[ $path == "$SDIR"/* ]] || warn "$path is outside $SDIR"
      if [[ -s $path ]] && ! yes_no "  $path exists. Replace it" n; then continue; fi
      read -rs -p "  Secret for [$sec] (input hidden): " S </dev/tty; echo
      [[ -n $S ]] || { warn "empty, skipped"; continue; }
      if ((PREVIEW)); then printf '  [preview] would write %s (mode 600, %d characters)\n' "$path" "${#S}"
      else printf '%s\n' "$S" | install -o root -g root -m 600 /dev/stdin "$path"; fi
      unset S
      ok "$path"
    done
    ;;
  2)
    AV="$(sudo -u "$RUN_AS" bash -lc 'command -v ansible-vault' </dev/null 2>/dev/null || command -v ansible-vault || true)"
    [[ -n $AV ]] || die "ansible-vault not found for user $RUN_AS (install ansible-core, or use option 1)"
    ok "using $AV"
    VAULT="$(ask "Path to your vault file" "")"
    sudo -u "$RUN_AS" test -r "$VAULT" </dev/null || die "$VAULT is not readable by $RUN_AS"
    VPASS="$(ask "Vault password file (leave empty to be asked)" "")"
    [[ -z $VPASS ]] || sudo -u "$RUN_AS" test -r "$VPASS" </dev/null || die "$VPASS is not readable by $RUN_AS"
    MAP=""
    for line in "${SECRETS[@]}"; do
      path="${line#*|}"; base="$(basename "$path")"
      case "$base" in
        pihole.pass) def=pihole_password;; jellyfin.key) def=jellyfin_api_key;;
        emby.key) def=emby_api_key;; plex.token) def=plex_token;; *) def="${base//./_}";;
      esac
      key="$(ask "Vault key name for $base" "$def")"
      [[ $key =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || die "bad key name: $key"
      MAP+="$key=$base "
      sed -i "s#= *$path\$#= $RAM_DIR/$base#" "$CFG"
    done
    sed -i "/^password_file = ${RAM_DIR//\//\\/}\/.*/a session_file = $RAM_DIR/pihole.sid" "$CFG"
    if ! grep -q '^\[vault\]' "$CFG"; then
      printf '\n[vault]\ndir = %s\nmap = %s\n' "$RAM_DIR" "${MAP% }" >>"$CFG"
    fi
    $PY -I -B "$SRC/collect.py" --config "$CFG" --check-config >/dev/null || die "config became invalid; please report this"
    ok "config now reads secrets from $RAM_DIR (RAM)"
    VIEW=("$AV" view); [[ -n $VPASS ]] && VIEW+=(--vault-password-file "$VPASS"); VIEW+=("$VAULT")
    if ((PREVIEW)); then
      printf '  [preview] would run: sudo -u %s %s | %s -I -B %s/unlock.py --config %s\n' "$RUN_AS" "${VIEW[*]}" "$PY" "$INSTALL_DIR" "$CFG_LIVE"
    else
      sudo -u "$RUN_AS" bash -lc "$(printf '%q ' "${VIEW[@]}")" | $PY -I -B "$INSTALL_DIR/unlock.py" --config "$CFG_LIVE"
    fi
    if [[ -d $CONF_DIR/secrets ]] && yes_no "Shred the old on-disk secrets in $CONF_DIR/secrets" y; then
      run find "$CONF_DIR/secrets" -type f -exec shred -u {} +
      run rmdir "$CONF_DIR/secrets"
    fi
    ;;
  *) warn "skipped: add them later (INSTALL.md step 4 or SECRETS-FROM-VAULT.md)";;
  esac
fi

# ------------------------------------------------------------------ dry run
step "Dry run of the collector (writes nothing)"
DRY_ERR="$WORK/dry.txt"
$PY -I -B "$CODE/collect.py" --config "$CFG" --dry-run 2>"$DRY_ERR" >/dev/null || die "dry run failed: $(tail -1 "$DRY_ERR")"
grep '^#' "$DRY_ERR" | sed 's/^/  /'
sed -n 's/^#   \[[^]]*\] //p' "$DRY_ERR" | sort >"$WORK/dry-errors.txt"
if [[ -s $WORK/dry-errors.txt ]]; then
  yes_no "Some checks have errors (listed above). Continue anyway" n || die "fix the config ($CFG_LIVE) and run this again"
else
  ok "every check worked"
fi

# -------------------------------------------------------------------- units
step "systemd units"
for u in homelab-dash-collector.service homelab-dash-collector.timer homelab-dash-web.service; do
  sed -e "s#/opt/homelab-dash#$INSTALL_DIR#g" -e "s#/etc/homelab-dash#$CONF_DIR#g" \
      -e "s#SocketBindAllow=tcp:8088#SocketBindAllow=tcp:$PORT#" "$SRC/systemd/$u" | put "/etc/systemd/system/$u" 644
done
ALLOW="$SUBNET"; ((TS)) && ALLOW+=" 100.64.0.0/10 fd7a:115c:a1e0::/48"
run install -d -o root -g root -m 755 /etc/systemd/system/homelab-dash-web.service.d
printf '# Written by the homelab-dash installer: networks allowed to reach the dashboard.\n[Service]\nIPAddressAllow=%s\n' "$ALLOW" \
  | put /etc/systemd/system/homelab-dash-web.service.d/10-network.conf 644
if [[ $D_HOME == 1 || $D_SMART == 0 ]]; then
  run install -d -o root -g root -m 755 /etc/systemd/system/homelab-dash-collector.service.d
  $PY -I -B "$SRC/detect.py" --collector-dropin | put /etc/systemd/system/homelab-dash-collector.service.d/10-local.conf 644
fi
run systemctl daemon-reload
if ((! PREVIEW)); then
  V="$(systemd-analyze verify /etc/systemd/system/homelab-dash-*.service /etc/systemd/system/homelab-dash-*.timer 2>&1 || true)"
  [[ -z $V ]] && ok "units verified" || { echo "$V" | sed 's/^/    /'; warn "systemd-analyze verify printed the above"; }
fi

step "First run inside the sandbox (timer still off)"
run systemctl start homelab-dash-collector.service
if ((! PREVIEW)); then
  SUM="$($PY -I -B "$INSTALL_DIR/collect.py" --config "$CFG_LIVE" --summary || true)"
  echo "$SUM" | sed 's/^/  /'
  echo "$SUM" | sed -n 's/^ *- //p' | sort >"$WORK/run-errors.txt"
  NEW="$(comm -13 "$WORK/dry-errors.txt" "$WORK/run-errors.txt")"
  if [[ -n $NEW ]]; then
    warn "these errors appear only inside the sandbox (see INSTALL.md Troubleshooting):"
    echo "$NEW" | sed 's/^/      /'
  else
    ok "sandbox blocks nothing that the checks need"
  fi
fi

step "Web server"
run systemctl enable --now homelab-dash-web.service
if ((! PREVIEW)); then
  sleep 1
  read -r C1 C2 C3 < <($PY - "$PORT" <<'EOF'
import sys, urllib.request, urllib.error
port = sys.argv[1]
def code(path, method="GET"):
    req = urllib.request.Request("http://127.0.0.1:%s%s" % (port, path), method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0
print(code("/"), code("/status.json", "POST"), code("/state.json"))
EOF
)
  [[ $C1 == 200 ]] && ok "page answers (200)" || die "page did not answer (got $C1): systemctl status homelab-dash-web"
  [[ $C2 == 405 && $C3 == 404 ]] && ok "read-only: POST refused (405), state.json hidden (404)" || warn "unexpected: POST=$C2 state.json=$C3"
  WEBUSER="$(ps -o user= -C python3 -o cmd= 2>/dev/null | awk '/serve.py/{print $1; exit}')"
  [[ -n $WEBUSER && $WEBUSER != root ]] && ok "runs as '$WEBUSER', not root" || warn "could not confirm the web server user"
fi

# ----------------------------------------------------------------- firewall
step "Firewall"
fw_rules() {  # fw_rules show|apply
  local act=$1
  case "$D_FW" in
    ufw)
      fw_do "$act" ufw allow from "$SUBNET" to any port "$PORT" proto tcp comment "homelab-dash LAN"
      ((TS)) && fw_do "$act" ufw allow in on tailscale0 to any port "$PORT" proto tcp comment "homelab-dash tailscale"
      ;;
    firewalld)
      fw_do "$act" firewall-cmd --permanent "--add-rich-rule=rule family=\"ipv4\" source address=\"$SUBNET\" port port=\"$PORT\" protocol=\"tcp\" accept"
      ((TS)) && fw_do "$act" firewall-cmd --permanent "--add-rich-rule=rule family=\"ipv4\" source address=\"100.64.0.0/10\" port port=\"$PORT\" protocol=\"tcp\" accept"
      fw_do "$act" firewall-cmd --reload
      ;;
  esac
  return 0
}
fw_do() { local act=$1; shift; if [[ $act == show ]]; then printf '    '; printf '%q ' "$@"; printf '\n'; else run "$@"; fi; }
if [[ $D_FW != ufw && $D_FW != firewalld ]]; then
  warn "no active firewall found. The systemd IPAddressAllow= limit still keeps the page to $ALLOW."
  warn "Not enabling a firewall automatically (that could lock you out of SSH); see INSTALL.md step 8."
else
  echo "  Rules to add ($D_FW):"
  fw_rules show
  if yes_no "Add them now" y; then fw_rules apply; ok "firewall rules added"
  else warn "skipped: add them later (INSTALL.md step 8)"; fi
fi

# -------------------------------------------------------------------- timer
step "Timer"
run systemctl enable --now homelab-dash-collector.timer
((PREVIEW)) || { systemctl list-timers homelab-dash-collector.timer --no-pager | sed -n 1,2p | sed 's/^/  /'; ok "collector runs about every 60 s"; }

step "Security score (lower is better)"
if ((! PREVIEW)); then
  systemd-analyze security homelab-dash-collector.service homelab-dash-web.service --no-pager 2>/dev/null | grep -E 'Overall' | sed 's/^/  /' || true
else
  for u in homelab-dash-collector.service homelab-dash-web.service; do
    systemd-analyze security --offline=true --no-pager "$SRC/systemd/$u" 2>/dev/null | grep Overall | sed 's/^/  /' || true
  done
fi

# --------------------------------------------------------------------- done
step "Done"
((PREVIEW)) && warn "that was a PREVIEW: nothing was installed. Run without --preview to install."
[[ -n $D_IP ]] && echo "  On your home network:   http://$D_IP:$PORT/"
((TS)) && [[ -n $D_TSNAME ]] && echo "  Over Tailscale:         http://$D_TSNAME:$PORT/"
echo "  Next: docs/TEST-PLAN.md (24-hour soak and reboot test)."
echo "  Undo everything: docs/INSTALL.md, section \"Rollback\"."
[[ ${MODE:-} == 2 ]] && echo "  After each reboot: ansible-vault view $VAULT | sudo $PY -I -B $INSTALL_DIR/unlock.py --config $CFG_LIVE"
exit 0
