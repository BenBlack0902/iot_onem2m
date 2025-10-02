# Cloud Hub (IN-CSE) Setup Instructions

This guide explains how to set up the WireGuard VPN hub on the cloud server.

## 1. Install WireGuard

First, ensure you are running as the `root` user. Then, update your package lists and install the necessary tools.

```bash
sudo apt-get update
sudo apt-get install wireguard -y
```

## 2. Copy the Configuration File

The `wg0.conf` file in this directory is pre-configured with the correct private key for the hub and the public keys for all spokes.

Copy this file to the WireGuard directory on your cloud server:

```bash
cp wg0.conf /etc/wireguard/wg0.conf
```

## 3. Open the Firewall

WireGuard listens on UDP port `51820`. You must allow incoming traffic on this port.

### Using `ufw` (Uncomplicated Firewall)

If you are using `ufw`, run the following command:

```bash
sudo ufw allow 51820/udp
```

### Using `iptables`

If you are managing `iptables` directly, add a rule to the INPUT chain:

```bash
sudo iptables -A INPUT -p udp --dport 51820 -j ACCEPT
```
*Note: You will need to ensure this `iptables` rule persists after a reboot. The method for this varies depending on your system configuration (e.g., using `iptables-persistent`).*

## 4. Enable and Start the WireGuard Service

Use `systemd` to enable the WireGuard service so it starts automatically on boot. The `@wg0` part of the command corresponds to the configuration file name `wg0.conf`.

```bash
sudo systemctl enable --now wg-quick@wg0
```

## 5. Verify the Status

You can check if the WireGuard interface is up and running with the following commands:

```bash
# Check the service status
sudo systemctl status wg-quick@wg0

# View the WireGuard interface and peer connections
sudo wg show
