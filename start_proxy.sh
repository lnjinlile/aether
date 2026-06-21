#!/bin/bash
# Aether Proxy - one command to start
# Prerequisite: place mihomo binary at ~/.local/bin/mihomo
CONFIG_DIR="$HOME/.config/aether-proxy"
mkdir -p "$CONFIG_DIR"

cat > "$CONFIG_DIR/config.yaml" << 'YAML'
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
    proxies: ["加拿大-Binance"]
rules:
  - DOMAIN-SUFFIX,binance.com,Proxy
  - DOMAIN-SUFFIX,binancefuture.com,Proxy
  - DOMAIN-SUFFIX,coingecko.com,Proxy
  - MATCH,DIRECT
YAML

if ! command -v mihomo &>/dev/null; then
    echo "❌ mihomo not found. Download from:"
    echo "   https://github.com/MetaCubeX/mihomo/releases"
    echo "   Place at ~/.local/bin/mihomo"
    exit 1
fi

mihomo -d "$CONFIG_DIR" &
sleep 2
curl -s --max-time 5 --proxy socks5h://127.0.0.1:7890 https://api.binance.com/api/v3/ping && echo "✅ 加拿大节点已连接" || echo "⏳ 启动中..."
