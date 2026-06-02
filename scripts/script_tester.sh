# ===== Windows 系（引数なし → windows-pc.example が対象） =====

# 1. プロセスデルタ（5秒かかる）
echo "--- win_process_delta.sh ---"
./win_process_delta.sh

# 2. イベントログ異常
echo "--- win_eventlog_anomaly.sh ---"
./win_eventlog_anomaly.sh

# 3. ディスク異常（C:\フルスキャンなので少し時間かかる）
echo "--- win_disk_anomaly.sh ---"
./win_disk_anomaly.sh

# ===== Linux 系（引数なし → localhost = monitoring-host自身）=====

# 4. メモリ圧力
echo "--- linux_memory_pressure.sh ---"
./linux_memory_pressure.sh

# 5. ディスク inode
echo "--- linux_disk_inode.sh ---"
./linux_disk_inode.sh

# 6. journal 異常
echo "--- linux_journal_anomaly.sh ---"
./linux_journal_anomaly.sh
