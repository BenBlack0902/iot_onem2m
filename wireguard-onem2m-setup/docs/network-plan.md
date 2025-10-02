# Network Plan

## VPN Network
- WireGuard subnet: 10.100.0.0/24
- Cloud hub IP: 10.100.0.1

## Cloud Server (Hetzner)
- Provider: Hetzner Cloud (Falkenstein)
- Public IP: 91.98.80.99
- OS: Debian
- SSH access: YES (root@91.98.80.99)
- Hostname: debian-8gb-fsn1-1

## Alper's Home
- VPN IP: 10.100.0.2
- Pi Model: [ASK ME]
- Home LAN subnet: 192.168.123.0/24
- Pi LAN IP: 192.168.123.238
- Router access: NO

## Benjamin's Home
- VPN IP: 10.100.0.3
- Pi Model: [ASK ME]
- Home LAN subnet: [ASK ME - format: 192.168.?.0/24]
- Pi LAN IP: [ASK ME - format: 192.168.?.?]
- Router access: NO

## Tahir's Home
- VPN IP: 10.100.0.4
- Pi Model: [ASK ME]
- Home LAN subnet: [ASK ME - format: 192.168.?.0/24]
- Pi LAN IP: [ASK ME - format: 192.168.?.?]
- Router access: NO

## Routing Decision
[ ] Option A: Add static routes on each home router (cleaner, requires router access) - NOT FEASIBLE
[x] Option B: Use NAT on each Pi (easier, works without router access) - SELECTED
