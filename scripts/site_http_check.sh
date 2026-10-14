#!/bin/bash
# site_http_check.sh - 外から見るサイトの調査（応答・所要時間の内訳・証明書・名前解決）
# 使い方: ./site_http_check.sh <URL>   例: ./site_http_check.sh https://example.jp/
# 相手のサーバには入らない（ssh しない）。監視サーバの側から読み直すだけ＝共用レンタルサーバでも使える。
set -u

URL="${1:-}"
case "$URL" in
    http://*|https://*) ;;
    *) echo "エラー: URL を http:// か https:// で渡すこと（受け取った値: '$URL'）" >&2; exit 2 ;;
esac

# URL からホスト名とポートを取り出す（user@ と パス以降を落とす）
HOSTPORT="${URL#*://}"
HOSTPORT="${HOSTPORT%%/*}"
HOSTPORT="${HOSTPORT##*@}"
HOST="${HOSTPORT%%:*}"
if [ "$HOSTPORT" != "$HOST" ]; then
    PORT="${HOSTPORT##*:}"
elif [ "${URL%%://*}" = "https" ]; then
    PORT=443
else
    PORT=80
fi

echo "===== 名前解決 ($HOST) ====="
if command -v getent >/dev/null 2>&1; then
    getent ahosts "$HOST" | awk '{print $1}' | sort -u | head -5 || true
    getent ahosts "$HOST" >/dev/null || echo "名前解決できない"
fi

echo ""
echo "===== 応答（3回・秒） ====="
for i in 1 2 3; do
    curl -sS -o /dev/null --max-time 20 \
        -w "code=%{http_code} dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} first_byte=%{time_starttransfer} total=%{time_total} redirects=%{num_redirects}\n" \
        -L --max-redirs 5 "$URL" 2>&1 | head -3
done

echo ""
echo "===== 応答ヘッダ（最初の応答のみ） ====="
curl -sS -I --max-time 20 "$URL" 2>&1 | head -15

if [ "${URL%%://*}" = "https" ]; then
    echo ""
    echo "===== 証明書 ====="
    if ! command -v openssl >/dev/null 2>&1; then
        echo "INFO: openssl が無いので証明書は読めない。"
    else
        CERT=$(echo | timeout 15 openssl s_client -connect "$HOST:$PORT" -servername "$HOST" 2>/dev/null \
            | openssl x509 -noout -subject -issuer -dates -ext subjectAltName 2>/dev/null)
        if [ -z "$CERT" ]; then
            echo "証明書を取れなかった（TLS の接続そのものが失敗している可能性）。"
        else
            echo "$CERT"
            END=$(echo "$CERT" | sed -n 's/^notAfter=//p')
            if [ -n "$END" ]; then
                DAYS=$(( ( $(date -d "$END" +%s) - $(date +%s) ) / 86400 ))
                echo "残り日数: ${DAYS} 日"
            fi
        fi
    fi
fi
