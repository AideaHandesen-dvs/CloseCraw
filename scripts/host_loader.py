#!/usr/bin/env python3
"""
collectspec の JSON を読み込み、監視対象ホスト一覧を返す。
"""
import json
import sys
from pathlib import Path
from typing import Any

def load_monitoring_targets(json_path: str | Path) -> list[dict[str, Any]]:
    """
    inventory.json を読み込み、監視対象ホストのリストを返す。

    対象条件:
        - ip が "unknown" でない
        - status が "offline" でない（キーがない場合はオンライン扱い）
    """
    with open(json_path, encoding="utf-8") as f:
        all_hosts = json.load(f)

    targets = []
    for host in all_hosts:
        ip = host.get("ip", "unknown")
        status = host.get("status", "online")
        if ip == "unknown" or status == "offline":
            continue
        # 必要最小限の情報だけ抽出（あとで拡張可能）
        targets.append(
            {
                "hostname": host["hostname"],
                "ip": ip,
                "os": host.get("os", ""),
                "role": host.get("role", ""),
                "memory_mb": host.get("memory_mb"),
                "memory_gb": host.get("memory_gb"),
            }
        )
    return targets

def classify_os(os_str: str) -> str:
    """OS文字列から 'linux', 'openwrt', 'windows', 'unknown' を返す"""
    if not os_str:
        return "unknown"
    os_lower = os_str.lower()
    if os_lower.startswith("openwrt"):
        return "openwrt"
    elif os_lower.startswith("windows"):
        return "windows"
    elif os_lower.startswith("debian"):
        return "linux"
    return "unknown"

def get_targets_by_os(json_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """監視対象をOS種別ごとに分類して返す"""
    targets = load_monitoring_targets(json_path)
    classified = {"linux": [], "openwrt": [], "windows": [], "unknown": []}
    for t in targets:
        os_type = classify_os(t.get("os", ""))
        classified[os_type].append(t)
    return classified

def classify_all_hosts(json_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """全ホストをOS種別ごとに分類して返す（オフライン含む）"""
    with open(json_path, encoding="utf-8") as f:
        all_hosts = json.load(f)
    classified = {"linux": [], "openwrt": [], "windows": [], "unknown": []}
    for host in all_hosts:
        os_type = classify_os(host.get("os", ""))
        classified[os_type].append(host)
    return classified

if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("CLOSECRAW_SPEC_FILE", "/opt/closecraw/inventory.json")

    print("=== 全ホストのOS分類（モデル全体） ===")
    all_classified = classify_all_hosts(json_path)
    for os_type, hosts in all_classified.items():
        print(f"\n{os_type.upper()}: {len(hosts)} hosts")
        for h in hosts:
            print(f"  - {h['hostname']} ({h.get('os', 'N/A')})")

    print("\n\n=== 監視対象ホストのOS分類（オンラインのみ） ===")
    online_classified = get_targets_by_os(json_path)
    for os_type, hosts in online_classified.items():
        print(f"\n{os_type.upper()}: {len(hosts)} hosts")
        for h in hosts:
            print(f"  - {h['hostname']} ({h.get('os', 'N/A')})")
