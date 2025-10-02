# Benjamin - WireGuard Spoke Setup

This README explains how to install WireGuard on the Raspberry Pi, place the supplied config, replace placeholders, enable the tunnel, and perform basic tests.

## 1. Install WireGuard
Run as root (or prefix with sudo):
```bash
sudo apt-get update
sudo apt-get install wireguard -y
```

## 2. Place the config
Copy the provided `wg0.conf` into the system location and set secure permissions:
```bash
sudo cp wg0.conf /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
```

## 3. Replace placeholders
Open `/etc/wireguard/wg0.conf` and replace the following placeholders:
- `[BENJAMIN_LAN_SUBNET]` → your home LAN subnet (e.g. `192.168.2.0/24`)
- `[ALPER_LAN_SUBNET]`, `[TAHIR_LAN_SUBNET]` → other homes' LAN subnets if/when provided

If the config contains a PrivateKey already, do NOT share that file publicly. Keep private keys on the device and the public keys in project docs.

## 4. NAT (Option B) notes
The config includes a PostUp rule that adds a MASQUERADE rule for traffic from your home LAN to the VPN. Confirm the source subnet you set in `[BENJAMIN_LAN_SUBNET]` is correct. If your LAN interface is not `eth0` (e.g., `wlan0`), update the PostUp/PostDown commands to use the correct interface name.

## 5. Enable and start the tunnel
Enable & start the wg-quick service:
```bash
sudo systemctl enable --now wg-quick@wg0
```

## 6. Verify the tunnel
Check service and peers:
```bash
sudo systemctl status wg-quick@wg0
sudo wg show
```

## 7. Test connectivity
From the Pi, test ping to the cloud hub VPN IP:
```bash
ping -c 4 10.100.0.1
```
From the cloud hub, you can `sudo wg show` to verify the peer is connected and last handshake time.

## 8. Reverting PostUp changes
If you need to remove the iptables rules manually (e.g., after testing), run:
```bash
sudo iptables -D FORWARD -i wg0 -o eth0 -j ACCEPT
sudo iptables -D FORWARD -i eth0 -o wg0 -j ACCEPT
sudo iptables -t nat -D POSTROUTING -s [BENJAMIN_LAN_SUBNET] -o wg0 -j MASQUERADE
sudo sysctl -w net.ipv4.ip_forward=0
```

## 9. Security note
- Keep `private.key` file permissions set to 600.
- Do not upload private keys to public repositories.
