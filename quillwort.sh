#!/usr/bin/env bash
# quillwort.sh: quick installer for homelab-dash (interactive or unattended).
#
#   sudo bash ./quillwort.sh                 # install, asking for paths and settings
#   sudo bash ./quillwort.sh --preview       # walk through everything, change NOTHING
#   sudo bash ./quillwort.sh --yes [flags]   # unattended: never prompts (see --help)
#
# Run it after INSTALL.md steps 0.1-1.1 (prerequisites, download). It then does
# INSTALL.md steps 1.2 to 11, in order, with the same checks: offline tests,
# code install, detection and config review, secrets, dry run, units and a
# sandboxed run, web server, firewall, timer, phone tips, security score.
# To undo everything: docs/INSTALL.md, section "Rollback".

set -Eeuo pipefail
umask 022

usage() {
  cat <<'EOF'
Usage: sudo bash ./quillwort.sh [--preview] [--yes] [options]

  --preview                 ask everything / show every command, change nothing
  --yes, -y                 unattended: never prompt; use the options below,
                            then the detected defaults
  --log FILE                log file (default /var/log/homelab-dash-install.log;
                            in --preview only written if --log is given)
  --no-log                  do not write a log file

Answers (each also readable from the environment variable shown):
  --install-dir DIR         HLDASH_INSTALL_DIR     default /opt/homelab-dash
  --config-dir DIR          HLDASH_CONFIG_DIR      default /etc/homelab-dash
  --port N                  HLDASH_PORT            default: first free from 8088
  --subnet CIDR             HLDASH_SUBNET          default: detected LAN
  --tailscale yes|no        HLDASH_TAILSCALE       default: yes if installed
  --keep-config yes|no      HLDASH_KEEP_CONFIG     default yes (if one exists)
  --secrets vault|skip|type HLDASH_SECRETS         unattended: vault or skip only
  --vault FILE              HLDASH_VAULT           Ansible vault file
  --vault-password-file [F] HLDASH_VAULT_PASSWORD_FILE  without F (or "prompt"), or left
                                                   out: ansible-vault asks for the vault
                                                   password, as in the interactive install
                                                   (needs a terminal); give F when there is none
  --vault-key FILE=KEY      HLDASH_VAULT_KEYS="pihole.pass=key ..."  (repeatable)
  --shred-old-secrets yes|no HLDASH_SHRED_OLD      default: yes asked / no with --yes
  --firewall yes|no         HLDASH_FIREWALL        add ufw/firewalld rules, default yes
  --lan-addresses yes|no    HLDASH_LAN_ADDRESSES   LAN addresses card (free macvlan IPs,
                                                   local domain links), default yes
  --domain-suffix NAME      HLDASH_DOMAIN_SUFFIX   default home.arpa
  --domain-scheme http|https HLDASH_DOMAIN_SCHEME  domain links open with, default http
  --pihole-url URL          HLDASH_PIHOLE_URL      turn on / add Pi-hole stats at this URL
  --jellyfin-url URL        HLDASH_JELLYFIN_URL    same for Jellyfin (also --emby-url,
  --plex-url URL            HLDASH_PLEX_URL        HLDASH_EMBY_URL) and Plex
  --continue-on-errors      HLDASH_CONTINUE_ON_ERRORS=yes  carry on after dry-run errors

Secret values are never accepted as options or variables: they come from a
hidden prompt (interactive) or from your Ansible vault.
EOF
}

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREVIEW=0
YES=0
LOG="/var/log/homelab-dash-install.log"
LOG_SET=0
NO_LOG=0
A_INSTALL="${HLDASH_INSTALL_DIR:-}"
A_CONF="${HLDASH_CONFIG_DIR:-}"
A_PORT="${HLDASH_PORT:-}"
A_SUBNET="${HLDASH_SUBNET:-}"
A_TS="${HLDASH_TAILSCALE:-}"
A_KEEP="${HLDASH_KEEP_CONFIG:-}"
A_SECRETS="${HLDASH_SECRETS:-}"
A_VAULT="${HLDASH_VAULT:-}"
A_VPASS="${HLDASH_VAULT_PASSWORD_FILE:-}"
A_VKEYS="${HLDASH_VAULT_KEYS:-}"
A_SHRED="${HLDASH_SHRED_OLD:-}"
A_FW="${HLDASH_FIREWALL:-}"
A_CONT="${HLDASH_CONTINUE_ON_ERRORS:-}"
A_LAN="${HLDASH_LAN_ADDRESSES:-}"
A_DSUF="${HLDASH_DOMAIN_SUFFIX:-}"
A_DSCH="${HLDASH_DOMAIN_SCHEME:-}"
declare -A A_URL=([pihole]="${HLDASH_PIHOLE_URL:-}" [jellyfin]="${HLDASH_JELLYFIN_URL:-}"
                  [emby]="${HLDASH_EMBY_URL:-}" [plex]="${HLDASH_PLEX_URL:-}")
need() { [[ $# -ge 2 && -n $2 ]] || { echo "option $1 needs a value (see --help)" >&2; exit 2; }; }
while (($#)); do
  case "$1" in
    --preview) PREVIEW=1;;
    --yes|-y) YES=1;;
    --log) need "$@"; LOG="$2"; LOG_SET=1; shift;;
    --no-log) NO_LOG=1;;
    --install-dir) need "$@"; A_INSTALL="$2"; shift;;
    --config-dir) need "$@"; A_CONF="$2"; shift;;
    --port) need "$@"; A_PORT="$2"; shift;;
    --subnet) need "$@"; A_SUBNET="$2"; shift;;
    --tailscale) need "$@"; A_TS="$2"; shift;;
    --keep-config) need "$@"; A_KEEP="$2"; shift;;
    --secrets) need "$@"; A_SECRETS="$2"; shift;;
    --vault) need "$@"; A_VAULT="$2"; shift;;
    --vault-password-file)   # the file is optional: without it, the password is asked for
      if [[ $# -ge 2 && -n $2 && $2 != --* ]]; then A_VPASS="$2"; shift; else A_VPASS=prompt; fi;;
    --vault-key) need "$@"; A_VKEYS+=" $2"; shift;;
    --shred-old-secrets) need "$@"; A_SHRED="$2"; shift;;
    --firewall) need "$@"; A_FW="$2"; shift;;
    --continue-on-errors) A_CONT=yes;;
    --lan-addresses) need "$@"; A_LAN="$2"; shift;;
    --domain-suffix) need "$@"; A_DSUF="$2"; shift;;
    --domain-scheme) need "$@"; A_DSCH="$2"; shift;;
    --pihole-url) need "$@"; A_URL[pihole]="$2"; shift;;
    --jellyfin-url) need "$@"; A_URL[jellyfin]="$2"; shift;;
    --emby-url) need "$@"; A_URL[emby]="$2"; shift;;
    --plex-url) need "$@"; A_URL[plex]="$2"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2;;
  esac
  shift
done
yn() {  # normalise yes/no answers given as options: yn VALUE -> y|n|""
  case "${1,,}" in y|yes|1|true|on) echo y;; n|no|0|false|off) echo n;; "") echo "";;
    *) echo "bad yes/no value: $1" >&2; exit 2;; esac
}
A_TS="$(yn "$A_TS")"; A_KEEP="$(yn "$A_KEEP")"; A_SHRED="$(yn "$A_SHRED")"
A_FW="$(yn "$A_FW")"; A_CONT="$(yn "$A_CONT")"; A_LAN="$(yn "$A_LAN")"
case "$A_DSCH" in ""|http|https) ;; *) echo "--domain-scheme must be http or https" >&2; exit 2;; esac
SUF_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'
if [[ -n $A_DSUF && ! ${A_DSUF,,} =~ $SUF_RE ]]; then echo "--domain-suffix must look like home.arpa" >&2; exit 2; fi
URL_RE='^https?://[][A-Za-z0-9.:-]+(/[^[:space:]]*)?$'   # host[:port] (IPv6 in [ ]), optional path
for k in pihole jellyfin emby plex; do
  if [[ -n ${A_URL[$k]} && ! ${A_URL[$k]} =~ $URL_RE ]]; then echo "--$k-url must be a URL like http://192.168.1.53:8081" >&2; exit 2; fi
done
A_URLS="${A_URL[pihole]}${A_URL[jellyfin]}${A_URL[emby]}${A_URL[plex]}"
case "$A_SECRETS" in ""|vault|skip|type) ;; *) echo "--secrets must be vault, skip or type" >&2; exit 2;; esac
if ((YES)) && [[ $A_SECRETS == type ]]; then
  echo "--secrets type needs a person at the keyboard; with --yes use vault or skip" >&2; exit 2
fi
case "$A_VPASS" in prompt|ask|-) A_VPASS="";; esac   # "" = ansible-vault asks for the password
have_tty() { { : </dev/tty; } 2>/dev/null; }
if ((YES)) && [[ $A_SECRETS == vault && -z $A_VPASS ]] && ! have_tty; then
  echo "--secrets vault: no terminal here to type the vault password; give --vault-password-file FILE" >&2; exit 2
fi

PY=/usr/bin/python3
STEP="start"
RUN_AS="${SUDO_USER:-root}"
RAM_DIR=/run/homelab-dash-secrets
DATA_DIR=/var/lib/homelab-dash   # fixed: comes from StateDirectory= in the unit

# --------------------------------------------------------------------- log
# Everything printed (never a secret value: those are only read with read -s
# or piped from the vault) is also appended to the log, without colours.
bold=$'\033[1m'; green=$'\033[32m'; yellow=$'\033[33m'; red=$'\033[31m'; off=$'\033[0m'
[[ -t 1 ]] || { bold=; green=; yellow=; red=; off=; }
TEE_PID=""
if ((NO_LOG == 0)) && { ((PREVIEW == 0)) || ((LOG_SET)); }; then
  if ((PREVIEW == 0)) && [[ $EUID -ne 0 ]]; then echo "run with sudo:  sudo bash $0" >&2; exit 1; fi
  install -d -m 755 "$(dirname "$LOG")"
  ( umask 077; touch "$LOG" ); chmod 600 "$LOG"
  exec 3>&1 4>&2
  printf '\n######## %s  homelab-dash install  user=%s  preview=%s  unattended=%s\n' \
    "$(date -Is)" "$RUN_AS" "$PREVIEW" "$YES" >>"$LOG"
  exec > >(tee -a "$LOG") 2>&1
  TEE_PID=$!
fi
finish() {
  if [[ -n $TEE_PID ]]; then
    exec 1>&3 2>&4
    wait "$TEE_PID" 2>/dev/null || true
    sed -i 's/\x1b\[[0-9;]*m//g' "$LOG" 2>/dev/null || true   # keep the log free of colour codes
  fi
  [[ -n ${WORK:-} ]] && rm -rf "$WORK"
  return 0
}
trap finish EXIT

# ------------------------------------------------------------------ helpers
step() { STEP="$*"; printf '\n%s== %s%s\n' "$bold" "$*" "$off"; }
ok()   { printf '  %s✓%s %s\n' "$green" "$off" "$*"; }
warn() { printf '  %s!%s %s\n' "$yellow" "$off" "$*"; }
die()  { printf '  %s✗ %s%s\n' "$red" "$*" "$off" >&2; exit 1; }
trap '((BASHPID == $$)) && printf "\n%s✗ stopped during: %s%s\n  Steps before this one were completed. To undo: docs/INSTALL.md \"Rollback\".\n" "$red" "$STEP" "$off" >&2' ERR

# run CMD...: execute, or only show it in --preview mode
run() {
  if ((PREVIEW)); then printf '  [preview] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
# write_root_file FILE MODE: write stdin to FILE, owned by root, with MODE.
# (Not `install /dev/stdin FILE`: the Rust coreutils of Ubuntu 25.10+ refuse that.)
write_root_file() {
  local tmp
  tmp="$(mktemp "$1.XXXXXX")" || return 1          # created 0600, next to FILE
  if cat >"$tmp" && chmod "$2" "$tmp" && chown root:root "$tmp" && mv -f "$tmp" "$1"; then return 0; fi
  rm -f "$tmp"; return 1
}
# put FILE MODE: write stdin to FILE (root-owned), or show it in --preview mode
put() {
  if ((PREVIEW)); then printf '  [preview] would write %s (mode %s):\n' "$1" "$2"; sed 's/^/      | /'; else
    write_root_file "$1" "$2"; fi
}
ask() {  # ask "Question" "default" -> answer on stdout. Unattended: the default, shown in the log.
  local ans
  if ((YES)); then printf '  %s: %s (unattended)\n' "$1" "${2:-}" >&2; printf '%s' "${2:-}"; return; fi
  read -r -p "  $1${2:+ [$2]}: " ans </dev/tty
  # the prompt itself reaches the log through tee; add the typed answer after it
  if [[ -n $ans ]]; then printf '%s\n' "$ans"; else printf '(default) %s\n' "${2:-}"; fi >>"${ANSWER_LOG:-/dev/null}"
  printf '%s' "${ans:-${2:-}}"
}
ask_path() {  # like ask, with Tab completion (readline) for file and folder names
  local ans
  if ((YES)); then printf '  %s: %s (unattended)\n' "$1" "${2:-}" >&2; printf '%s' "${2:-}"; return; fi
  read -e -r -p "  $1${2:+ [$2]}: " ans </dev/tty
  if [[ -n $ans ]]; then printf '%s\n' "$ans"; else printf '(default) %s\n' "${2:-}"; fi >>"${ANSWER_LOG:-/dev/null}"
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
# typed answers are echoed by the terminal, not printed, so record them in the log too
ANSWER_LOG=""; [[ -n $TEE_PID ]] && ANSWER_LOG="$LOG"
((YES)) && warn "UNATTENDED mode: no questions; options, then detected defaults are used"

# --------------------------------------------------------------- preflight
step "Before step 1.2: check the prerequisites (steps 0.1 and 1.1)"
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

step "Step 1.2: offline tests (fake data, nothing touched)"
TEST_OUT="$(mktemp /tmp/homelab-dash-tests.XXXXXX)"
if [[ $RUN_AS != root ]]; then
  NOT_MINE="$(find "$SRC" ! -user "$RUN_AS" -print -quit 2>/dev/null || true)"
  [[ -n $NOT_MINE ]] && warn "some files in $SRC are not owned by $RUN_AS (e.g. $NOT_MINE). If the tests fail, run:  sudo chown -R $RUN_AS: $SRC"
  TEST_CMD=(sudo -u "$RUN_AS" bash -c "cd '$SRC' && $PY -B -m unittest discover -s tests")
else
  TEST_CMD=(bash -c "cd '$SRC' && $PY -B -m unittest discover -s tests")
fi
echo "  running the offline tests as $RUN_AS (about 20-60 s) ..."
if "${TEST_CMD[@]}" </dev/null >"$TEST_OUT" 2>&1; then
  grep -E '^(Ran|OK)' "$TEST_OUT" | sed 's/^/  /'
  rm -f "$TEST_OUT"
  ok "tests passed"
else
  grep -E '^(Ran|FAILED)' "$TEST_OUT" | sed 's/^/  /' || true
  echo "  Failing tests:"
  grep -E '^(FAIL|ERROR):' "$TEST_OUT" | sed 's/^/    /' || echo "    (none named: the test run itself crashed)"
  echo "  Last lines of the output:"
  tail -n 25 "$TEST_OUT" | sed 's/^/    | /'
  echo "  Full output saved in $TEST_OUT  (read it with: sudo less $TEST_OUT)"
  echo "  Run the tests yourself, as $RUN_AS:  cd $SRC && python3 -m unittest discover -s tests -v"
  die "offline tests failed; nothing was installed (details above)"
fi
printf '  Step 1.3 (optional fake-data demo) is skipped. To try it later: python3 %s/tests/demo.py --port 8099\n' "$SRC"

# ------------------------------------------------------------- the questions
step "Detect this server for the default answers (read-only)"
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

step "Your choices (used by steps 2-9)"
INSTALL_DIR="$(ask_path "Install the code to" "${A_INSTALL:-/opt/homelab-dash}")";   safe_dir "$INSTALL_DIR"
CONF_DIR="$(ask_path "Config folder" "${A_CONF:-/etc/homelab-dash}")";             safe_dir "$CONF_DIR"
printf '  Data folder is fixed: %s\n' "$DATA_DIR"
# An earlier install keeps its port by default (phones and bookmarks use it).
CUR_PORT="$($PY - "$CONF_DIR/config.ini" <<'EOF' 2>/dev/null || true
import configparser, sys
cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
cp.read(sys.argv[1])
print(cp.get("web", "port", fallback="").strip())
EOF
)"
is_ours() {  # is_ours PORT: does the dashboard's own web server answer there?
  $PY - "$1" <<'EOF' 2>/dev/null
import sys, urllib.request
req = urllib.request.Request("http://127.0.0.1:%s/" % sys.argv[1], method="HEAD")
try:
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as r:
        sys.exit(0 if r.headers.get("Server") == "homelab-dash" else 1)
except Exception:
    sys.exit(1)
EOF
}
DEF_PORT="${A_PORT:-${CUR_PORT:-$D_PORT}}"
[[ -z $A_PORT && -z $CUR_PORT ]] && is_ours 8088 && DEF_PORT=8088   # running already, config gone
PORT="$(ask "Dashboard port" "$DEF_PORT")"
[[ $PORT =~ ^[0-9]+$ ]] && ((PORT >= 1024 && PORT <= 65535)) || die "port must be 1024-65535"
if ! $PY -c 'import socket,sys; s=socket.socket(); s.bind(("0.0.0.0", int(sys.argv[1])))' "$PORT" 2>/dev/null; then
  if is_ours "$PORT"; then
    ok "port $PORT is used by the dashboard that is already running; it will be restarted with the new settings"
  else
    die "port $PORT is already in use by another program; pick another (sudo ss -tlnp | grep :$PORT shows which)"
  fi
fi
if [[ -n $CUR_PORT && $CUR_PORT != "$PORT" ]]; then
  warn "the port changes from $CUR_PORT to $PORT: home-screen icons and bookmarks need the new port,"
  warn "and the firewall rules for $CUR_PORT stay until you delete them (INSTALL.md Rollback R4 shows how)"
fi
SUBNET="$(ask "Home network allowed to open the page" "${A_SUBNET:-$D_SUBNET}")"
$PY -c 'import ipaddress,sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$SUBNET" 2>/dev/null \
  || die "'$SUBNET' is not a network like 192.168.1.0/24"
SUBNET="$($PY -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))' "$SUBNET")"
if yes_no "Also allow access over Tailscale" "${A_TS:-$D_TS}"; then TS=1; else TS=0; fi

echo
printf '  %-22s %s\n' "Code:" "$INSTALL_DIR" "Config:" "$CONF_DIR" "Data:" "$DATA_DIR" \
  "Port:" "$PORT" "Allowed:" "$SUBNET$([[ $TS == 1 ]] && echo ' + Tailscale')"
yes_no "Continue with these settings" y || die "cancelled; nothing was changed"

WORK="$(mktemp -d)"   # removed by finish() on exit
CFG_LIVE="$CONF_DIR/config.ini"
CFG="$CFG_LIVE"; ((PREVIEW)) && CFG="$WORK/config.ini"
CODE="$INSTALL_DIR"; ((PREVIEW)) && CODE="$SRC"   # preview runs the code from the repo

# ------------------------------------------------------------------ install
step "Step 2: install the code (root-owned, read-only)"
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

step "Step 3: config folder, draft, review, validate"
run install -d -o root -g root -m 755 "$CONF_DIR"
NEW_CONFIG=1
if [[ -f $CFG_LIVE ]]; then
  if yes_no "Keep the existing $CFG_LIVE" "${A_KEEP:-y}"; then
    NEW_CONFIG=0; ((PREVIEW)) && cp "$CFG_LIVE" "$CFG"
    # The units and firewall use the port answered above: the kept config must match it.
    if [[ ${CUR_PORT:-8088} != "$PORT" ]]; then
      ((PREVIEW)) || cp -p "$CFG_LIVE" "$CFG_LIVE.bak.$(date +%Y%m%d-%H%M%S)"
      $PY - "$CFG" "$PORT" <<'EOF' || die "could not set the port in $CFG_LIVE"
import re, sys
path, port = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out, in_web, done = [], False, False
for line in lines:
    if re.match(r"^\s*\[", line):
        if in_web and not done:
            out.append("port = %s" % port); done = True
        in_web = line.strip() == "[web]"
    elif in_web and re.match(r"^\s*port\s*=", line):
        line = "port = %s" % port; done = True
    out.append(line)
if not done:
    if not in_web:
        out += ["", "[web]"]
    out.append("port = %s" % port)
open(path, "w").write("\n".join(out) + "\n")
EOF
      ok "kept your config, with [web] port set to $PORT to match your answer ($( ((PREVIEW)) && echo 'preview: not saved' || echo 'backup made'))"
    fi
    [[ -n $A_LAN$A_DSUF$A_DSCH ]] && warn "kept your config: LAN addresses options not applied (edit [addresses] and [domains] in it)"
    [[ -n $A_URLS ]] && warn "kept your config: --pihole-url/--jellyfin-url/--emby-url/--plex-url not applied (edit it, or answer 'n' to keep it)"
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
  # LAN addresses card (free IPs on macvlan/ipvlan networks, local domains as links)
  if yes_no "Show the LAN addresses card (free macvlan IPs, local domain links)" "${A_LAN:-y}"; then
    DSUF="$(ask "Local domain suffix" "${A_DSUF:-home.arpa}")"; DSUF="${DSUF,,}"; DSUF="${DSUF#.}"
    [[ $DSUF =~ $SUF_RE ]] || die "'$DSUF' is not a domain suffix like home.arpa"
    DSCH="$(ask "Open domain links with (http/https)" "${A_DSCH:-http}")"
    [[ $DSCH == http || $DSCH == https ]] || die "answer http or https"
    sed -i -e "/^\[domains\]/,/^\[/ s/^suffix = .*/suffix = $DSUF/" \
           -e "/^\[domains\]/,/^\[/ s/^scheme = .*/scheme = $DSCH/" "$DRAFT"
    ok "LAN addresses card on: *.$DSUF, links open with $DSCH"
  else
    sed -i -e '/^\[addresses\]/,/^\[/ s/^enabled = .*/enabled = no/' \
           -e '/^\[domains\]/,/^\[/ s/^enabled = .*/enabled = no/' "$DRAFT"
    ok "LAN addresses card off"
  fi

  # Stats cards that need a secret. The detector switches a card off when it can't
  # reach the service (e.g. Pi-hole on a macvlan IP), so ask here: step 4 can only
  # ask for the vault or the passwords for cards that are on.
  echo "  Stats cards that need a secret (Pi-hole, Jellyfin, Emby, Plex):"
  reach_check() {  # warn (don't fail) if the URL's host:port doesn't answer from here
    $PY -c 'import socket,sys,urllib.parse as u; p=u.urlparse(sys.argv[1]); socket.create_connection((p.hostname, p.port or (443 if p.scheme=="https" else 80)), 2).close()' "$1" 2>/dev/null \
      || warn "$1 does not answer from this server yet: the card shows an error until it does (macvlan? see INSTALL.md 'macvlan')"
  }
  stat_label() { case "$1" in pihole) echo "Pi-hole";; jellyfin) echo "Jellyfin";; emby) echo "Emby";; plex) echo "Plex";; *) echo "$1";; esac; }
  enable_stat() {  # enable_stat KIND URL
    [[ $2 =~ $URL_RE ]] || die "'$2' is not a URL like http://192.168.1.53:8081"
    $PY -I -B "$SRC/detect.py" --draft-enable "$DRAFT" "$1" "$2" "$CONF_DIR" || die "could not update the draft"
    ok "$(stat_label "$1") stats on: $2"; reach_check "$2"
  }
  MISSING=()
  while IFS='|' read -r kind state url reason; do
    label="$(stat_label "$kind")"
    furl="${A_URL[$kind]}"
    case "$state" in
      on)
        ok "$label stats: on ($url)"
        [[ -n $furl && $furl != "$url" ]] && enable_stat "$kind" "$furl"
        ;;
      off)
        warn "$label stats are off in the draft: $reason${url:+ (url $url)}"
        if yes_no "Turn on $label stats" "$( [[ -n $furl ]] && echo y || { ((YES)) && echo n || echo y; } )"; then
          u="$(ask "$label URL, reachable from this server (Enter = leave it off)" "${furl:-$url}")"
          if [[ -n $u ]]; then enable_stat "$kind" "$u"; else warn "$label stats left off"; fi
        fi
        ;;
      missing)
        if [[ -n $furl ]]; then enable_stat "$kind" "$furl"; else MISSING+=("$kind"); fi
        ;;
    esac
  done < <($PY -I -B "$SRC/detect.py" --draft-stats "$DRAFT")
  if ((${#MISSING[@]})) && ((! YES)); then
    ADD="$(ask "Add stats for one that runs elsewhere? (${MISSING[*]}; Enter = none)" "")"
    for kind in $ADD; do
      [[ " ${MISSING[*]} " == *" $kind "* ]] || { warn "'$kind' skipped: choose from ${MISSING[*]}"; continue; }
      u="$(ask "$(stat_label "$kind") URL, reachable from this server (Enter = skip)" "")"
      if [[ -n $u ]]; then enable_stat "$kind" "$u"; else warn "$(stat_label "$kind") skipped"; fi
    done
  fi
  ED="${EDITOR:-$(command -v nano || command -v vi || echo vi)}"
  while :; do
    if ((YES)); then :   # unattended: no editor; the draft must validate as written
    elif yes_no "Open the draft in $ED to review it (recommended)" y; then
      $ED "$DRAFT" </dev/tty >/dev/tty
    fi
    if grep -E '^[^#]*(password|token|api_?key) *=' "$DRAFT" | grep -vE '_file *=|^ *map *=' >/dev/null; then
      ((YES)) && die "the config must not contain secret values, only *_file paths"
      warn "the config must not contain secret values, only *_file paths"; continue
    fi
    if $PY -I -B "$SRC/collect.py" --config "$DRAFT" --check-config; then break; fi
    ((YES)) && die "config draft is not valid (see above); run interactively to fix it"
    yes_no "Fix it in the editor" y || die "config not valid; nothing installed beyond the code"
  done
  if ((PREVIEW)); then cp "$DRAFT" "$CFG"; ok "(preview) config kept in a temp folder"
  else install -o root -g root -m 644 "$DRAFT" "$CFG_LIVE"; ok "$CFG_LIVE"; fi
fi

# ------------------------------------------------------------------ secrets
step "Step 4: secrets"
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
  ok "no enabled card needs a secret, so there is nothing to ask (no vault, no passwords)"
  echo "  Pi-hole, Jellyfin, Emby and Plex stats are off in $CFG_LIVE."
  echo "  To add them: re-run and answer 'n' to keeping the config, or enable them there and see SECRETS-FROM-VAULT.md."
  [[ -n $A_SECRETS$A_VAULT ]] && warn "--secrets/--vault were given but not used"
else
  echo "  Cards that need a secret:"
  for line in "${SECRETS[@]}"; do printf '    [%s]  %s\n' "${line%%|*}" "${line#*|}"; done
  echo "  How do you want to provide them?"
  echo "    1) type them now (stored in root-only files, mode 600)"
  echo "    2) from an Ansible vault, kept in RAM only (unlock again after each reboot)"
  echo "    3) skip for now (those cards show an error until you add them)"
  case "$A_SECRETS" in type) DEF=1;; vault) DEF=2;; skip) DEF=3;; *) DEF=1; [[ -n $A_VAULT ]] && DEF=2;; esac
  ((YES)) && [[ -z $A_SECRETS ]] && die "these cards need secrets: pass --secrets vault (with --vault) or --secrets skip"
  MODE="$(ask "Choice" "$DEF")"
  echo "  secrets mode: $MODE"
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
      else printf '%s\n' "$S" | write_root_file "$path" 600 || die "could not write $path"; fi
      unset S
      ok "$path"
    done
    ;;
  2)
    AV="$(sudo -u "$RUN_AS" bash -lc 'command -v ansible-vault' </dev/null 2>/dev/null || command -v ansible-vault || true)"
    [[ -n $AV ]] || die "ansible-vault not found for user $RUN_AS (install ansible-core, or use option 1)"
    ok "using $AV"
    VAULT="$(ask_path "Path to your vault file (Tab completes)" "$A_VAULT")"
    [[ -n $VAULT ]] || die "no vault file given"
    sudo -u "$RUN_AS" test -r "$VAULT" </dev/null || die "$VAULT is not readable by $RUN_AS"
    VPASS="$(ask_path "Vault password file (leave empty to be asked)" "$A_VPASS")"
    [[ -z $VPASS ]] || sudo -u "$RUN_AS" test -r "$VPASS" </dev/null || die "$VPASS is not readable by $RUN_AS"
    MAP=""
    for line in "${SECRETS[@]}"; do
      path="${line#*|}"; base="$(basename "$path")"
      case "$base" in
        pihole.pass) def=pihole_password;; jellyfin.key) def=jellyfin_api_key;;
        emby.key) def=emby_api_key;; plex.token) def=plex_token;; *) def="${base//./_}";;
      esac
      for kv in $A_VKEYS; do [[ ${kv%%=*} == "$base" ]] && def="${kv#*=}"; done
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
      [[ -z $VPASS ]] && echo "  ansible-vault now asks for your vault password (typed, not shown):"
      sudo -u "$RUN_AS" bash -lc "$(printf '%q ' "${VIEW[@]}")" | $PY -I -B "$INSTALL_DIR/unlock.py" --config "$CFG_LIVE"
    fi
    SHRED_DEF="${A_SHRED:-$( ((YES)) && echo n || echo y)}"
    if [[ -d $CONF_DIR/secrets ]] && yes_no "Shred the old on-disk secrets in $CONF_DIR/secrets" "$SHRED_DEF"; then
      run find "$CONF_DIR/secrets" -type f -exec shred -u {} +
      run rmdir "$CONF_DIR/secrets"
    fi
    ;;
  *) warn "skipped: add them later (INSTALL.md step 4 or SECRETS-FROM-VAULT.md)";;
  esac
fi

# ------------------------------------------------------------------ dry run
step "Step 5: dry run of the collector (writes nothing)"
DRY_ERR="$WORK/dry.txt"
$PY -I -B "$CODE/collect.py" --config "$CFG" --dry-run 2>"$DRY_ERR" >/dev/null || die "dry run failed: $(tail -1 "$DRY_ERR")"
grep '^#' "$DRY_ERR" | sed 's/^/  /'
sed -n 's/^#   \[[^]]*\] //p' "$DRY_ERR" | sort >"$WORK/dry-errors.txt"
if [[ -s $WORK/dry-errors.txt ]]; then
  yes_no "Some checks have errors (listed above). Continue anyway" "${A_CONT:-n}" || die "fix the config ($CFG_LIVE) and run this again"
else
  ok "every check worked"
fi

# -------------------------------------------------------------------- units
step "Steps 6.1-6.5: systemd units and drop-ins"
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
  N="$(systemctl cat homelab-dash-web.service 2>/dev/null | grep -c '^IPAddressAllow=' || true)"
  [[ $N == 2 ]] && ok "network drop-in loaded (IPAddressAllow in unit + drop-in)" || warn "expected 2 IPAddressAllow= lines, found $N"
  # verify also loads unrelated units and may complain about them: keep only ours
  V="$(systemd-analyze verify /etc/systemd/system/homelab-dash-*.service /etc/systemd/system/homelab-dash-*.timer 2>&1 | grep 'homelab-dash' || true)"
  [[ -z $V ]] && ok "units verified" || { echo "$V" | sed 's/^/    /'; warn "systemd-analyze verify printed the above"; }
fi

step "Step 6.6: first run inside the sandbox (timer still off)"
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

step "Step 7: web server"
run systemctl enable homelab-dash-web.service
# restart (not just start): a web server left running by an earlier install must
# pick up the new port, units and config
run systemctl restart homelab-dash-web.service
if ((! PREVIEW)); then
  printf '  waiting for the web server to answer on port %s (up to 20 s) ' "$PORT"
  for _ in $(seq 1 20); do
    $PY -c 'import socket,sys; socket.create_connection(("127.0.0.1", int(sys.argv[1])), 1).close()' "$PORT" 2>/dev/null && break
    printf '.'; sleep 1
  done; echo
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
  if [[ $C1 != 200 ]]; then
    echo "  The page did not answer (HTTP code ${C1:-none}; 0 = no connection). What systemd says:"
    systemctl status homelab-dash-web --no-pager 2>&1 | head -n 12 | sed 's/^/    | /' || true
    echo "  Last log lines:"
    journalctl -u homelab-dash-web -n 25 --no-pager 2>&1 | sed 's/^/    | /' || true
    die "web server not answering on port $PORT (details above)"
  fi
  ok "page answers (200)"
  [[ $C2 == 405 && $C3 == 404 ]] && ok "read-only: POST refused (405), state.json hidden (404)" || warn "unexpected: POST=$C2 state.json=$C3"
  WEBUSER="$(ps -o user= -C python3 -o cmd= 2>/dev/null | awk '/serve.py/{print $1; exit}')"
  [[ -n $WEBUSER && $WEBUSER != root ]] && ok "runs as '$WEBUSER', not root" || warn "could not confirm the web server user"
fi

# ----------------------------------------------------------------- firewall
step "Step 8: firewall (home network and Tailscale only)"
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
  if yes_no "Add them now" "${A_FW:-y}"; then
    fw_rules apply
    if ((! PREVIEW)); then
      if [[ $D_FW == ufw ]]; then FWN="$(ufw status | grep -c " $PORT/tcp" || true)"
      else FWN="$(firewall-cmd --list-rich-rules | grep -c "port=\"$PORT\"" || true)"; fi
      ((FWN >= 1 + TS)) && ok "firewall rules active for port $PORT ($FWN found)" || warn "expected $((1 + TS)) rule(s) for port $PORT, found $FWN"
    fi
  else warn "skipped: add them later (INSTALL.md step 8)"; fi
fi

# -------------------------------------------------------------------- timer
step "Step 9: timer"
run systemctl enable --now homelab-dash-collector.timer
if ((! PREVIEW)); then
  systemctl list-timers homelab-dash-collector.timer --no-pager | sed -n 1,2p | sed 's/^/  /'
  T0="$(date +%s)"; FRESH=0
  printf '  waiting for the timer'"'"'s first run (up to 2 minutes) '
  for _ in $(seq 1 24); do
    GEN="$($PY -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["generated_at"]))' "$DATA_DIR/status.json" 2>/dev/null || echo 0)"
    if ((GEN >= T0)); then FRESH=1; break; fi
    printf '.'; sleep 5
  done; echo
  ((FRESH)) && ok "timer ran the collector; data is fresh" || warn "no new data yet: check  journalctl -u homelab-dash-collector -n 20"
fi

step "Step 10: add it to your phone's home screen (on the phone)"
echo "  iPhone (Safari): open the page, Share, Add to Home Screen."
echo "  Android (Chrome): open the page, menu, Add to Home screen."

step "Step 11: security score (lower is better)"
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
[[ -n $TEE_PID ]] && echo "  Log of this run: $LOG"
echo "  Next: docs/TEST-PLAN.md (24-hour soak and reboot test)."
echo "  Undo everything: docs/INSTALL.md, section \"Rollback\"."
[[ ${MODE:-} == 2 ]] && echo "  After each reboot: ansible-vault view $VAULT | sudo $PY -I -B $INSTALL_DIR/unlock.py --config $CFG_LIVE"
exit 0
