#!/usr/bin/env bash
# Egress lockdown: force an agent user's outbound traffic through the gateway.
#
# WHAT THIS DOES
#   Installs an OS-level firewall rule so that every outbound connection made
#   by the agent OS user ($AGENT_USER) -- and every child process it spawns,
#   including a hand-typed `curl` -- can reach ONLY the gateway host:port.
#   Any other destination is dropped by the kernel, below the application, so
#   it does not matter whether the traffic went through the PreToolUse hook.
#
# WHY THIS IS THE SECURITY FLOOR (not the hook)
#   The PreToolUse hook is a cooperative, application-level checkpoint: code
#   that does not trip it (a subprocess of an allowed tool, an MCP server that
#   shells out, an injected tool that execs curl) bypasses it. This rule sits
#   in the OS network path, so a bypass attempt has only two outcomes and
#   neither succeeds:
#     1. Aimed at an external host  -> dropped by this rule (never leaves box).
#     2. Aimed at the gateway host  -> the gateway receives it and, being
#        default-deny, rejects anything that is not an authorized tool call.
#   So the gateway never has to *read* the bytes of an opaque `curl`: opaque
#   traffic either has nowhere legal to go, or lands on a default-deny gateway.
#   This is what turns the "no raw side channels" precondition
#   (docs/SELF_HOSTING.md) from an *assumption* into an *enforced* property.
#
# HARD REQUIREMENT -- THE AGENT USER MUST BE NON-ADMIN
#   This rule is only as strong as the agent's inability to remove it. If the
#   agent runs as root / an admin / a user with sudo, it (or code it is tricked
#   into running) can flush these rules and bypass the gateway entirely. Run
#   the agent (Claude Code, Codex, ...) as a DEDICATED NON-ADMIN user with no
#   sudo. Giving the agent admin privileges voids this control -- see the
#   WARNING printed at the end and docs/SELF_HOSTING.md.
#
# WHAT THIS DOES NOT DO
#   - It does not cover non-network side effects (local file tampering, staging
#     secrets for a later authorized call). Constrain the filesystem separately.
#   - It does not give the gateway its egress; run the gateway as a DIFFERENT
#     user so its outbound calls to real SaaS are not caught by this rule.
#
# USAGE (run as root / with sudo):
#   sudo AGENT_USER=pauth-agent GATEWAY_HOST=127.0.0.1 GATEWAY_PORT=8081 \
#     gateway/deploy/egress_lockdown.sh apply
#   sudo AGENT_USER=pauth-agent gateway/deploy/egress_lockdown.sh status
#   sudo AGENT_USER=pauth-agent gateway/deploy/egress_lockdown.sh remove

set -euo pipefail

ACTION="${1:-apply}"
AGENT_USER="${AGENT_USER:-}"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
GATEWAY_PORT="${GATEWAY_PORT:-8081}"
ANCHOR="pauth_egress"   # nft table / iptables chain / pf anchor name

die() { echo "egress-lockdown: $*" >&2; exit 1; }

[[ -n "$AGENT_USER" ]] || die "set AGENT_USER to the dedicated non-admin agent account"
[[ "$(id -u)" -eq 0 ]] || die "must run as root (use sudo)"

AGENT_UID="$(id -u "$AGENT_USER")" || die "unknown user '$AGENT_USER'"

# Validate GATEWAY_HOST/PORT: they are interpolated into firewall commands and,
# on macOS, written verbatim into a pf.conf anchor that pfctl parses. Accept only
# a bare IP literal and a numeric port so nothing pf- or shell-significant (a
# rule fragment that widens the policy to allow-all, i.e. fail-open) can reach
# the ruleset.
[[ "$GATEWAY_PORT" =~ ^[0-9]+$ ]] && (( GATEWAY_PORT >= 1 && GATEWAY_PORT <= 65535 )) \
  || die "GATEWAY_PORT must be an integer 1-65535, got '$GATEWAY_PORT'"
if [[ "$GATEWAY_HOST" == *:* ]]; then
  [[ "$GATEWAY_HOST" =~ ^[0-9A-Fa-f:]+$ ]] || die "GATEWAY_HOST must be a bare IPv6 literal, got '$GATEWAY_HOST'"
else
  [[ "$GATEWAY_HOST" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "GATEWAY_HOST must be a bare IPv4 literal, got '$GATEWAY_HOST'"
fi

# Refuse to lock down a privileged account: the control would be self-defeating.
if [[ "$AGENT_UID" -eq 0 ]]; then
  die "'$AGENT_USER' is root; an admin agent can undo this rule. Use a non-admin user."
fi

# IPv6 gateway host if it contains a colon; otherwise IPv4.
GW_IS_V6=0
[[ "$GATEWAY_HOST" == *:* ]] && GW_IS_V6=1

warn_admin() {
  cat >&2 <<WARN

================================ WARNING ================================
 Egress lockdown is active for user '$AGENT_USER' (uid $AGENT_UID).
 This ONLY holds while that user is NON-ADMIN and cannot edit firewall
 rules or use sudo. If the agent is given admin/root privileges it can
 flush these rules and reach any external server directly, BYPASSING the
 gateway. Do not run the agent as an admin user.
========================================================================
WARN
}

# --------------------------------------------------------------------------
# Linux: nftables (preferred) or iptables (fallback)
# --------------------------------------------------------------------------
linux_apply_nft() {
  nft list table inet "$ANCHOR" >/dev/null 2>&1 && nft delete table inet "$ANCHOR"
  nft add table inet "$ANCHOR"
  nft add chain inet "$ANCHOR" out "{ type filter hook output priority 0 ; policy accept ; }"
  # Constrain ONLY the agent uid; everything else is untouched.
  nft add rule inet "$ANCHOR" out meta skuid != "$AGENT_UID" accept
  if [[ "$GW_IS_V6" -eq 1 ]]; then
    nft add rule inet "$ANCHOR" out meta skuid "$AGENT_UID" ip6 daddr "$GATEWAY_HOST" tcp dport "$GATEWAY_PORT" accept
  else
    nft add rule inet "$ANCHOR" out meta skuid "$AGENT_UID" ip daddr "$GATEWAY_HOST" tcp dport "$GATEWAY_PORT" accept
  fi
  # Everything else from the agent (other hosts, DNS, UDP/QUIC, the other IP
  # family) is dropped. The agent does not need external DNS: the gateway makes
  # the real SaaS calls itself.
  nft add rule inet "$ANCHOR" out meta skuid "$AGENT_UID" drop
}

linux_apply_iptables() {
  local ipt="$1" family_v6="$2"
  "$ipt" -N "$ANCHOR" 2>/dev/null || "$ipt" -F "$ANCHOR"
  if [[ "$family_v6" -eq 0 && "$GW_IS_V6" -eq 0 ]]; then
    "$ipt" -A "$ANCHOR" -d "$GATEWAY_HOST" -p tcp --dport "$GATEWAY_PORT" -j ACCEPT
  elif [[ "$family_v6" -eq 1 && "$GW_IS_V6" -eq 1 ]]; then
    "$ipt" -A "$ANCHOR" -d "$GATEWAY_HOST" -p tcp --dport "$GATEWAY_PORT" -j ACCEPT
  fi
  "$ipt" -A "$ANCHOR" -j REJECT
  "$ipt" -C OUTPUT -m owner --uid-owner "$AGENT_UID" -j "$ANCHOR" 2>/dev/null \
    || "$ipt" -A OUTPUT -m owner --uid-owner "$AGENT_UID" -j "$ANCHOR"
}

linux_apply() {
  if command -v nft >/dev/null 2>&1; then
    linux_apply_nft
  elif command -v iptables >/dev/null 2>&1; then
    linux_apply_iptables iptables 0
    command -v ip6tables >/dev/null 2>&1 && linux_apply_iptables ip6tables 1
  else
    die "no nft or iptables found"
  fi
}

linux_remove() {
  if command -v nft >/dev/null 2>&1; then
    nft list table inet "$ANCHOR" >/dev/null 2>&1 && nft delete table inet "$ANCHOR" || true
  fi
  for ipt in iptables ip6tables; do
    command -v "$ipt" >/dev/null 2>&1 || continue
    "$ipt" -D OUTPUT -m owner --uid-owner "$AGENT_UID" -j "$ANCHOR" 2>/dev/null || true
    "$ipt" -F "$ANCHOR" 2>/dev/null || true
    "$ipt" -X "$ANCHOR" 2>/dev/null || true
  done
}

linux_status() {
  command -v nft >/dev/null 2>&1 && nft list table inet "$ANCHOR" 2>/dev/null || true
  command -v iptables >/dev/null 2>&1 && iptables -S "$ANCHOR" 2>/dev/null || true
}

# --------------------------------------------------------------------------
# macOS: pf (supports per-user rules for locally-originated traffic)
# --------------------------------------------------------------------------
MAC_ANCHOR_FILE="/etc/pf.anchors/${ANCHOR}"

MAC_BEGIN="# BEGIN ${ANCHOR} (managed by egress_lockdown.sh)"
MAC_END="# END ${ANCHOR}"

macos_apply() {
  [[ "$GW_IS_V6" -eq 0 ]] || die "IPv6 gateway host on macOS pf is not scripted here; set an IPv4 GATEWAY_HOST"
  cat > "$MAC_ANCHOR_FILE" <<PF
# Managed by gateway/deploy/egress_lockdown.sh -- do not edit by hand.
block drop out proto { tcp udp } from any to any user $AGENT_UID
pass out proto tcp from any to $GATEWAY_HOST port $GATEWAY_PORT user $AGENT_UID
PF
  # Wire the anchor into the main ruleset between sentinels (idempotent: strip an
  # old block first). Validate the resulting ruleset BEFORE loading it, and back
  # up the original -- a broken edit must never leave pf loaded with the block
  # rule stripped (fail-open egress).
  cp /etc/pf.conf "/etc/pf.conf.pauth.bak.$$"
  _macos_strip_block > /etc/pf.conf.tmp
  {
    printf '%s\n' "$MAC_BEGIN"
    printf 'anchor "%s"\nload anchor "%s" from "%s"\n' "$ANCHOR" "$ANCHOR" "$MAC_ANCHOR_FILE"
    printf '%s\n' "$MAC_END"
  } >> /etc/pf.conf.tmp
  if ! pfctl -n -f /etc/pf.conf.tmp; then
    rm -f /etc/pf.conf.tmp
    die "pf.conf failed validation; original left untouched (backup: /etc/pf.conf.pauth.bak.$$)"
  fi
  mv /etc/pf.conf.tmp /etc/pf.conf
  pfctl -f /etc/pf.conf || die "pfctl load failed; egress NOT locked -- investigate before running the agent"
  pfctl -e 2>/dev/null || true
}

# Print /etc/pf.conf with any prior BEGIN..END sentinel block removed.
_macos_strip_block() {
  awk -v b="$MAC_BEGIN" -v e="$MAC_END" '
    $0==b {skip=1; next} $0==e {skip=0; next} !skip {print}
  ' /etc/pf.conf
}

macos_remove() {
  rm -f "$MAC_ANCHOR_FILE"
  # Strip only our sentinel-delimited block (never a substring match).
  if grep -qF "$MAC_BEGIN" /etc/pf.conf 2>/dev/null; then
    _macos_strip_block > /etc/pf.conf.tmp
    if ! pfctl -n -f /etc/pf.conf.tmp; then
      rm -f /etc/pf.conf.tmp
      die "pf.conf failed validation after strip; left untouched"
    fi
    mv /etc/pf.conf.tmp /etc/pf.conf
    pfctl -f /etc/pf.conf || die "pfctl reload failed after remove"
  fi
}

macos_status() {
  pfctl -a "$ANCHOR" -s rules 2>/dev/null || echo "(no $ANCHOR anchor loaded)"
}

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
OS="$(uname -s)"
case "$ACTION" in
  apply)
    case "$OS" in
      Linux)  linux_apply ;;
      Darwin) macos_apply ;;
      *) die "unsupported OS: $OS" ;;
    esac
    echo "egress-lockdown: applied for '$AGENT_USER' -> ${GATEWAY_HOST}:${GATEWAY_PORT} only"
    warn_admin
    ;;
  remove)
    case "$OS" in
      Linux)  linux_remove ;;
      Darwin) macos_remove ;;
      *) die "unsupported OS: $OS" ;;
    esac
    echo "egress-lockdown: removed for '$AGENT_USER'"
    ;;
  status)
    case "$OS" in
      Linux)  linux_status ;;
      Darwin) macos_status ;;
      *) die "unsupported OS: $OS" ;;
    esac
    ;;
  *)
    die "unknown action '$ACTION' (use: apply | remove | status)"
    ;;
esac
