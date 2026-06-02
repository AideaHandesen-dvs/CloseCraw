#!/bin/bash
# linux_memory_pressure.sh - OOM予兆のためのメモリ圧力情報を収集
# 使い方: ./linux_memory_pressure.sh [target_host]
set -u

TARGET="${1:-localhost}"
if [ "$TARGET" != "localhost" ]; then
    ssh "$TARGET" 'bash -s' < "$0"
    exit $?
fi

# --- 以下、診断先ホスト上で実行される ---
echo "===== /proc/pressure/memory ====="
if [ -r /proc/pressure/memory ]; then
    cat /proc/pressure/memory
else
    echo "エラー: /proc/pressure/memory が存在しないか読めません。"
fi

echo ""
echo "===== /proc/vmstat (oom_kill関連) ====="
if [ -r /proc/vmstat ]; then
    grep '^oom_kill' /proc/vmstat || echo "oom_kill行は見つかりませんでした。"
else
    echo "エラー: /proc/vmstat が読めません。"
fi

echo ""
echo "===== dmesg (oom/Out of memory) 最新20行 ====="
if dmesg 2>/dev/null | grep -iE 'oom|out of memory' | tail -n 20; then
    : # 成功
else
    if command -v journalctl >/dev/null 2>&1; then
        journalctl -k --no-pager -n 20 --grep='oom|Out of memory' 2>/dev/null || echo "journalctlでもOOMログを取得できませんでした。"
    else
        echo "dmesgもjournalctlも利用できず、OOMログを取得できません。"
    fi
fi
