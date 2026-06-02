#!/bin/bash
# win_eventlog_anomaly.sh - 直近のイベントログエラー・警告を種類別に集計
# 使い方: ./win_eventlog_anomaly.sh [target_host]
set -u
HOST="${1:-windows-pc.example}"
USER="${CLOSECRAW_WIN_USER:-your_windows_username}"
LOOKBACK_HOURS=1
MAX_EVENTS=1000
TOP_N=10

PWCMD="\$ProgressPreference='SilentlyContinue';
\$events = Get-WinEvent -FilterHashtable @{
    LogName='System';
    Level=2,3;
    StartTime=(Get-Date).AddHours(-${LOOKBACK_HOURS})
} -MaxEvents ${MAX_EVENTS} -ErrorAction SilentlyContinue;
if (\$events.Count -eq 0) {
    Write-Output 'No error or warning events in the last ${LOOKBACK_HOURS} hour(s).'
} else {
    \$events | Group-Object Id |
        Sort-Object Count -Descending |
        Select-Object -First ${TOP_N} |
        ForEach-Object {
            \$id = \$_.Name;
            \$count = \$_.Count;
            \$latest = (\$_.Group | Sort-Object TimeCreated -Descending)[0];
            \$msg = \$latest.Message -replace \"\`n\",' ' -replace \"\`r\",'';
            if(\$msg.Length -gt 100) { \$msg = \$msg.Substring(0,100) + '...' };
            Write-Output \"EventID: \$id, Count: \$count, Latest: \$(\$latest.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')), Message: \$msg\"
        }
}"

ssh "${USER}@${HOST}" powershell -NoProfile -EncodedCommand \
    "$(printf '%s' "$PWCMD" | iconv -t UTF-16LE | base64 -w0)"
