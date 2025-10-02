#!/bin/bash

# This script generates WireGuard private and public keys for all peers in the network.
# It saves the keys in the corresponding configs directory.

# --- Configuration ---
# An array of all peer locations. These names must match the directory names in ../configs/
PEERS=("cloud" "alper" "benjamin" "tahir")
CONFIG_DIR="../configs"

# --- Color Codes ---
COLOR_GREEN='\033[0;32m'
COLOR_YELLOW='\033[1;33m'
COLOR_RED='\033[0;31m'
COLOR_CYAN='\033[0;36m'
COLOR_NONE='\033[0m'

# --- Functions ---

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to generate a keypair for a single peer
generate_keypair() {
    local peer_name=$1
    local peer_dir="${CONFIG_DIR}/${peer_name}"
    local private_key_file="${peer_dir}/private.key"
    local public_key_file="${peer_dir}/public.key"

    echo -e "${COLOR_CYAN}--- Processing Peer: ${peer_name} ---${COLOR_NONE}"

    # Create the directory for the peer if it doesn't exist
    mkdir -p "${peer_dir}"

    # Check if keys already exist
    if [[ -f "${private_key_file}" || -f "${public_key_file}" ]]; then
        echo -e "${COLOR_YELLOW}WARNING: Keys already exist for ${peer_name}.${COLOR_NONE}"
        read -p "Do you want to overwrite them? (y/N) " -n 1 -r
        echo # Move to a new line
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Skipping key generation for ${peer_name}."
            echo
            return
        fi
        echo "Overwriting existing keys for ${peer_name}."
    fi

    # Generate the keys
    # umask is used to ensure the private key file has 600 permissions from the start.
    (umask 077 && wg genkey > "${private_key_file}")
    wg pubkey < "${private_key_file}" > "${public_key_file}"

    # Set explicit permissions just in case
    chmod 600 "${private_key_file}"

    echo -e "${COLOR_GREEN}Successfully generated keys for ${peer_name}.${COLOR_NONE}"
    echo
}

# --- Main Script ---

# 1. Check for wireguard-tools
echo "Checking for required tools..."
if ! command_exists wg; then
    echo -e "${COLOR_RED}ERROR: 'wireguard-tools' is not installed.${COLOR_NONE}"
    echo "Please install it to proceed. On Debian/Ubuntu, use:"
    echo "sudo apt-get update && sudo apt-get install wireguard-tools"
    exit 1
fi
echo -e "${COLOR_GREEN}Required tools are installed.${COLOR_NONE}\n"

# 2. Generate keys for all peers
for peer in "${PEERS[@]}"; do
    generate_keypair "${peer}"
done

# 3. Print summary of public keys
echo -e "${COLOR_CYAN}--- Public Key Summary ---${COLOR_NONE}"
for peer in "${PEERS[@]}"; do
    public_key_file="${CONFIG_DIR}/${peer}/public.key"
    if [[ -f "${public_key_file}" ]]; then
        public_key=$(cat "${public_key_file}")
        # Use printf for aligned output
        printf "%-10s: %s\n" "${peer}" "${public_key}"
    else
        printf "%-10s: ${COLOR_RED}Key not found!${COLOR_NONE}\n" "${peer}"
    fi
done

echo -e "\n${COLOR_GREEN}Key generation process complete.${COLOR_NONE}"
echo "Copy these public keys to your network plan and WireGuard configurations."
