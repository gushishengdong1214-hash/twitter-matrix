"""vnstat 月流量上报。需要 worker VPS 提前装 vnstat。"""

import json
import subprocess


def get_monthly_traffic_gb(interface: str = "") -> float:
    """读取 vnstat 当月流量(rx+tx),单位 GB。失败返回 0。"""
    cmd = ["vnstat", "-m", "--json"]
    if interface:
        cmd.extend(["-i", interface])
    try:
        out = subprocess.check_output(cmd, text=True, timeout=10)
    except Exception:
        return 0.0

    try:
        data = json.loads(out)
        for iface in data.get("interfaces", []):
            months = iface.get("traffic", {}).get("month", [])
            if not months:
                continue
            this = months[-1]
            rx = this.get("rx", 0)
            tx = this.get("tx", 0)
            return (rx + tx) / (1024 ** 3)
    except Exception:
        return 0.0
    return 0.0


if __name__ == "__main__":
    print(f"当月流量:{get_monthly_traffic_gb():.2f} GB")
