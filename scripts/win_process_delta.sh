#!/bin/bash
# win_process_delta.sh - 5秒間のワーキングセット増加トップ10
# 使い方: ./win_process_delta.sh [target_host]
set -u
HOST="${1:-windows-pc.example}"
USER="${CLOSECRAW_WIN_USER:-your_windows_username}"

PSCODE=$(cat << 'EOF' | iconv -f UTF-8 -t UTF-16LE | base64 -w0
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Continue'
$before = Get-Process -ErrorAction SilentlyContinue | Select Name, Id, @{N='WS';E={$_.WorkingSet64}}
Start-Sleep 5
$after = Get-Process -ErrorAction SilentlyContinue | Select Name, Id, @{N='WS';E={$_.WorkingSet64}}
$results = foreach ($p in $before) {
    $a = $after | Where-Object { $_.Id -eq $p.Id }
    if ($a -and $a.WS -gt $p.WS) {
        [PSCustomObject]@{
            ProcessName   = $p.Name
            PID           = $p.Id
            DeltaMB       = [math]::Round(($a.WS - $p.WS) / 1MB, 4)
            WS_BeforeMB   = [math]::Round($p.WS / 1MB, 2)
            WS_AfterMB    = [math]::Round($a.WS / 1MB, 2)
        }
    }
}
if ($results) {
    $results | Sort-Object DeltaMB -Descending | Select -First 10 | Format-Table -AutoSize | Out-String -Width 4096
} else {
    Write-Output "No process increased WorkingSet in 5 seconds."
}
EOF
)

ssh "${USER}@${HOST}" powershell -NoProfile -EncodedCommand "$PSCODE"
