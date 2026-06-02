#!/usr/bin/env python3
"""
collectspec JSON から Prometheus の static_configs を生成する。
"""
import json
import sys
from host_loader import load_monitoring_targets

def generate_prometheus_targets(json_path: str) -> dict:
    targets = load_monitoring_targets(json_path)
    linux_targets = []
    windows_targets = []

    for host in targets:
        ip = host["ip"]
        os_name = host["os"].lower()
        if "windows" in os_name:
            # windows-pc only - windows_exporter
            windows_targets.append(f"{ip}:9182")
        else:
            # Linux / OpenWrt など
            linux_targets.append(f"{ip}:9100")

    config = {
        "scrape_configs": [
            {
                "job_name": "node_exporter",
                "static_configs": [
                    {"targets": linux_targets, "labels": {"os": "linux"}}
                ]
            },
            {
                "job_name": "windows_exporter",
                "static_configs": [
                    {"targets": windows_targets, "labels": {"os": "windows"}}
                ]
            }
        ]
    }
    return config

if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "inventory.json"
    config = generate_prometheus_targets(json_path)
    print(json.dumps(config["scrape_configs"], indent=2))
