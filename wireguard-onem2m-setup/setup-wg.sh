#!/bin/bash
set -e

echo "=== Installing WireGuard ==="
sudo apt update -y
sudo apt install -y wireguard iptables ufw curl

# Detect main network interface (eth0, ens3, etc.)
NET_IF=$(ip route get 8.8.8.8 | awk '{for(i=1;i<=NF;i++){if($i=="dev"){print $(i+1);exit}}}')

echo "=== Creating WireGuard config ==="
sudo mkdir -p /etc/wireguard
sudo tee /etc/wireguard/wg0.conf > /dev/null <<EOF
[Interface]
Address = 10.100.0.1/24
ListenPort = 51820
PrivateKey = aObUwOO3ocYn3kN1XJWaVu5RpAZCJ2AG1Vo0NQX+xlQ=
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -t nat -A POSTROUTING -o ${NET_IF} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -t nat -D POSTROUTING -o ${NET_IF} -j MASQUERADE

[Peer]
# Alper
PublicKey = VjtPF8pxIXJMUs//96E0Y7kv4K6rTjLyyjR9VnPfBHU=
AllowedIPs = 10.100.0.2/32, 192.168.123.0/24

[Peer]
# Benjamin
PublicKey = ompdo0Gc+oenG2KH98asvMwEKDMruOs3bjd484SS4nk=
AllowedIPs = 10.100.0.3/32, 192.168.2.0/24

[Peer]
# Tahir
PublicKey = lZgTAy9zmjWDE/Ji4f3u61178HMoH/9iYdwR+HqMxTA=
AllowedIPs = 10.100.0.4/32, 192.168.3.0/24
EOF

sudo chmod 600 /etc/wireguard/wg0.conf

echo "=== Enabling and starting wg0 ==="
sudo systemctl enable wg-quick@wg0
sudo wg-quick down wg0 2>/dev/null || true
sudo wg-quick up wg0

echo "=== Opening firewall port ==="
sudo ufw allow 51820/udp || true

echo "=== Checking WireGuard status ==="
sudo wg show

PUB_IP=$(curl -s ifconfig.me)
echo
echo "✅ WireGuard is now running on interface wg0"
echo "   Public IP: $PUB_IP"
echo
echo "👉 Send this line to Alper so he can connect:"
echo
echo "Endpoint = ${PUB_IP}:51820"
echo
echo "To verify his connection later, run: sudo wg show"
