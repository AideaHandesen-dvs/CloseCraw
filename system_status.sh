#!/bin/bash
echo "=== Dockerコンテナ ==="
docker ps --format "{{.Names}}\t{{.Status}}"

echo ""
echo "=== Prometheusターゲット ==="
curl -s http://localhost:9090/api/v1/targets | \
  jq -r '.data.activeTargets[] | "\(.health)\t\(.labels.instance)"' | sort

echo ""
echo "=== 発火中のアラート ==="
curl -s http://localhost:9090/api/v1/alerts | \
  jq -r '.data.alerts[] | "\(.state)\t\(.labels.alertname)\t\(.labels.instance)"' \
  || echo "なし"

echo ""
echo "=== Alertmanager ==="
curl -s http://localhost:9093/-/healthy

echo ""
echo "=== alert_handler.py ==="
systemctl --user is-active alert-handler.service
curl -s http://localhost:5001/health

echo ""
echo "=== ollama ==="
curl -s http://server1.example:11434/api/tags | jq -r '.models[].name' 2>/dev/null

echo ""
echo "=== windows-pc SSH ==="
ssh -o ConnectTimeout=5 windows-pc "echo OK" 2>/dev/null || echo "FAILED"

echo ""
echo "=== 直近ログ ==="
journalctl --user -u alert-handler.service --no-pager -n 20
