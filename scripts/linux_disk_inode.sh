#!/bin/bash
# linux_disk_inode.sh - ディスク容量・inode・削除済み未解放ファイルの調査
# 使い方: ./linux_disk_inode.sh [target_host]
set -u

TARGET="${1:-localhost}"
if [ "$TARGET" != "localhost" ]; then
    ssh "$TARGET" 'bash -s' < "$0"
    exit $?
fi

echo "=== DISK USAGE (df -h) ==="
df -h -x tmpfs -x devtmpfs -x squashfs

echo ""
echo "=== INODE USAGE (df -i) ==="
df -i -x tmpfs -x devtmpfs -x squashfs

echo ""
echo "=== DELETED FILES HELD OPEN (top 10 by size) ==="
if ! command -v lsof >/dev/null 2>&1; then
    echo "INFO: lsof がインストールされていません。削除済み未解放ファイルは確認できません。"
else
    lsof -nP 2>/dev/null | grep -i 'deleted' | awk '{print $1,$2,$7,$NF}' | sort -t' ' -k3 -rn | head -10 || echo "該当するファイルはありません。"
fi
