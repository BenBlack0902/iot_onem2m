#!/usr/bin/env bash
# WireGuard connection tester (works on hub or any Pi)
# Usage: sudo bash scripts/04-test-connection.sh
# Checks: wg0 presence, wg status, VPN IP, peer handshakes, ping tests, wg0 routes
# Output: color-coded (green ✓ success, red ✗ failure, yellow ! warning)
#
# Note: script must be made executable: chmod +x scripts/04-test-connection.sh

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()    { printf "${GREEN}✓${NC} %b\n" "$1"; }
fail()  { printf "${RED}✗${NC} %b\n" "$1"; }
warn()  { printf "${YELLOW}!${NC} %b\n" "$1"; }

echo "WireGuard connection test - $(date)"
echo

# 1) Check if interface wg0 exists
if ip link show wg0 >/dev/null 2>&1; then
  ok "Interface wg0 exists"
else
  fail "Interface wg0 not found"
fi

# 2) Check if wg command is usable and WireGuard active
if wg show >/dev/null 2>&1; then
  ok "WireGuard is active (wg show OK)"
else
  fail "WireGuard not active or 'wg' command failed"
fi

# 3) Print assigned VPN IP (on wg0)
vpn_ip=$(ip -o -f inet addr show wg0 2>/dev/null | awk '{print $4}')
if [ -n "$vpn_ip" ]; then
  ok "VPN IP: $vpn_ip"
else
  warn "No VPN IP assigned to wg0"
fi

# 4) Print peer handshake status (show brief)
echo
echo "---- WireGuard status (wg show) ----"
if wg show >/dev/null 2>&1; then
  wg show
else
  echo "(wg show unavailable)"
fi
echo "------------------------------------"
echo

# 5-8) Ping tests
targets=(10.100.0.1 10.100.0.2 10.100.0.3 10.100.0.4)
names=("Cloud (10.100.0.1)" "Alper (10.100.0.2)" "Benjamin (10.100.0.3)" "Tahir (10.100.0.4)")
reachable=0
total=0

for i in "${!targets[@]}"; do
  t="${targets[$i]}"
  name="${names[$i]}"
  # Skip ping to self if target equals local VPN IP address (optional)
  if [ -n "$vpn_ip" ] && echo "$vpn_ip" | grep -q "${t%/*}"; then
    warn "Skipping ping to self ($name)"
    continue
  fi
  total=$((total+1))
  if ping -c 2 -W 2 "$t" >/dev/null 2>&1; then
    ok "Ping $name succeeded"
    reachable=$((reachable+1))
  else
    fail "Ping $name failed"
  fi
done

# 9) Show routing table entries for wg0
echo
if ip route | grep -q wg0; then
  ok "Routing entries for wg0 present"
  ip route | grep wg0 || true
else
  warn "No wg0 routes found"
fi

# Summary
echo
printf "Summary: "
if [ "$total" -gt 0 ]; then
  printf "${GREEN}%d${NC}/${total} nodes reachable\n" "$reachable"
else
  printf "${YELLOW}No ping targets were tested (local node equals target?)${NC}\n"
fi

exit 0
