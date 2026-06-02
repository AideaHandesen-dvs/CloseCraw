#!/bin/bash
# linux_journal_anomaly.sh - 直近15分のエラーログをランキング表示
# 使い方: ./linux_journal_anomaly.sh [target_host]
set -u

TARGET="${1:-localhost}"
if [ "$TARGET" != "localhost" ]; then
    ssh "$TARGET" 'bash -s' < "$0"
    exit $?
fi

echo "===== 直近15分のエラーログ (頻度順) ====="

# journalctl からエラーログを取得
if ! OUTPUT=$(journalctl -p err --since "15 min ago" -o cat --no-pager 2>/dev/null); then
    echo "journalctl の実行に失敗しました。"
    exit 1
fi

if [ -z "$OUTPUT" ]; then
    echo "直近15分のエラーログはありません。"
    exit 0
fi

# 出現回数順にソート
echo "$OUTPUT" | sort | uniq -c | sort -nr | head -10
