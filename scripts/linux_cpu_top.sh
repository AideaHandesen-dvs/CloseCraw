#!/bin/bash
# linux_cpu_top.sh - CPU高騰の原因調査（負荷の内訳・瞬間の消費者・待ちの所在）
# 使い方: ./linux_cpu_top.sh [target_host]
#
# なぜ必要か: CPU / load のアラートに linux_memory_pressure.sh を当てていたため、
# CPU アラートの通知が「メモリプレッシャーや OOM は観測されていない」で埋まっていた
# （実運用でそうなった）。CPU の話は CPU の材料で埋める。
set -u

TARGET="${1:-localhost}"
if [ "$TARGET" != "localhost" ]; then
    ssh "$TARGET" 'bash -s' < "$0"
    exit $?
fi

echo "=== LOAD AVERAGE (uptime) ==="
uptime

echo ""
echo "=== CPU CORES ==="
nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo "不明"

echo ""
echo "=== CPU PRESSURE (PSI: some=一部が待たされている / full=全部止まった) ==="
if [ -r /proc/pressure/cpu ]; then
    cat /proc/pressure/cpu
    echo "※ some=一部のタスクが CPU 待ちだった時間の割合(%)。avg10/avg60/avg300 が高いほど"
    echo "  継続的に待たされている。total は累積なので単体では判断できない（前回との差分で見る）。"
else
    echo "INFO: /proc/pressure/cpu が無い（カーネル 4.20 未満 or PSI 無効）。"
fi

echo ""
echo "=== CPU BREAKDOWN (vmstat 1秒×3: us/sy/id/wa の内訳) ==="
if command -v vmstat >/dev/null 2>&1; then
    vmstat 1 3
    echo "※ wa が高い＝CPU ではなく I/O 待ち。r が cores を超えて並ぶ＝実行待ちの行列。"
else
    echo "INFO: vmstat がインストールされていません。"
fi

echo ""
echo "=== TOP CPU CONSUMERS (瞬間値) ==="
# ps の %CPU は「プロセス起動からの平均」なので、長時間動く常駐が必ず上位に来る。
# top の2サンプル目（1秒差分）で瞬間値を見る。取れなければ ps で代用する。
if command -v top >/dev/null 2>&1; then
    # -w 200: 桁幅が狭いと COMMAND が「pipewir+」のように切れて読めない（busybox の top は
    #   -w を知らないので、その場合は OUT が空になり ps へ落ちる）。
    OUT=$(top -bn2 -d 1 -w 200 2>/dev/null | awk '/^ *PID +USER/{n++} n==2' | head -n 15)
    if [ -n "$OUT" ]; then
        echo "$OUT"
    else
        echo "INFO: top の2サンプル目が取れないため、ps の起動来平均で代用します。"
        ps -eo pcpu,pmem,rss,etime,comm --sort=-pcpu 2>/dev/null | head -n 15
    fi
else
    echo "INFO: top がありません。ps の起動来平均で代用します。"
    ps -eo pcpu,pmem,rss,etime,comm --sort=-pcpu 2>/dev/null | head -n 15
fi

echo ""
echo "=== 割り込み不可状態(D)のプロセス (I/O 待ちで固まっていないか) ==="
ps -eo stat,pid,comm 2>/dev/null | awk '$1 ~ /^D/' | head -n 10 || true
echo "（何も出なければ D 状態のプロセスは無し）"
