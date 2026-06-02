#!/bin/bash
# win_disk_anomaly.sh - 直近24時間に更新された大容量ファイルを検出
# 使い方: ./win_disk_anomaly.sh [target_host]
set -u
HOST="${1:-windows-pc.example}"
USER="${CLOSECRAW_WIN_USER:-your_windows_username}"
THRESHOLD_MB=100  # 100MB以上

PWCMD="\$ProgressPreference = 'SilentlyContinue';
\$thresholdBytes = ${THRESHOLD_MB} * 1MB;
\$limitTime = (Get-Date).AddHours(-24);
Write-Output 'Searching files >= ${THRESHOLD_MB}MB updated within 24h...';
\$files = Get-ChildItem -Path C:\ -Directory | ForEach-Object {
    Get-ChildItem \$_.FullName -Recurse -File -ErrorAction SilentlyContinue | 
    Where-Object { \$_.Length -ge \$thresholdBytes -and \$_.LastWriteTime -ge \$limitTime }
} | Sort-Object Length -Descending | Select-Object -First 50;
if (\$files) {
    \$files | Format-Table FullName, @{N='SizeMB';E={[math]::Round(\$_.Length/1MB,2)}}, LastWriteTime -AutoSize | Out-String -Width 4096
} else {
    Write-Output 'No large files found.'
}"

ssh "${USER}@${HOST}" powershell -NoProfile -EncodedCommand \
    "$(printf '%s' "$PWCMD" | iconv -t UTF-16LE | base64 -w0)"
