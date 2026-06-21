#!/bin/bash
# Aether Proxy Setup - Canadian node for Binance mainnet access
set -e

CONFIG_DIR="$HOME/.config/aether-proxy"
mkdir -p "$CONFIG_DIR"

# Write the Clash config (Canadian node only + Binance rule)
cat > "$CONFIG_DIR/config.yaml" << 'EOF'
mixed-port: 7890
log-level: info
mode: rule

proxies:
  - name: "加拿大-Binance"
    password: "3cc27cef-08ad-456f-8c60-d88f6590cb25"
    port: 32039
    server: "0cfbd3c0-fdc0-4f26-82fb-5c8f8b6ce82b.bnsepserv.com"
    skip-cert-verify: true
    sni: "pull-flv-t13-admin.douyincdn.com"
    type: trojan
    udp: true

proxy-groups:
  - name: "Proxy"
    type: select
    proxies:
      - "加拿大-Binance"

rules:
  - DOMAIN-SUFFIX,binance.com,Proxy
  - DOMAIN-SUFFIX,binancefuture.com,Proxy
  - DOMAIN-SUFFIX,binance.org,Proxy
  - DOMAIN-SUFFIX,coingecko.com,Proxy
  - MATCH,DIRECT
EOF

# Download mihomo if not present
if ! command -v mihomo &>/dev/null; then
    echo "Downloading mihomo..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then MARCH="amd64"
    elif [ "$ARCH" = "aarch64" ]; then MARCH="arm64"
    else echo "Unsupported arch: $ARCH"; exit 1; fi
    
    URL="https://github.com/MetaCubeX/mihomo/releases/download/v1.18.12/mihomo-linux-${MARCH}-v1.18.12.gz"
    curl -sL "$URL" -o /tmp/mihomo.gz
    gunzip -f /tmp/mihomo.gz
    chmod +x /tmp/mihomo
    mkdir -p "$HOME/.local/bin"
    mv /tmp/mihomo "$HOME/.local/bin/mihomo"
    export PATH="$HOME/.local/bin:$PATH"
fi

# Start proxy
echo "Starting proxy on port 7890..."
mihomo -d "$CONFIG_DIR" &
sleep 2

# Test
echo "Testing Binance mainnet..."
curl -s --max-time 5 --proxy socks5h://127.0.0.1:7890 https://api.binance.com/api/v3/ping && echo "✅ Done" || echo "Starting... might need a moment"
