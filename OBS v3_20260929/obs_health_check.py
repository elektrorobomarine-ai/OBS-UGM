"""Read centralized OBS acquisition health from shared_data without opening any OBS socket."""
from __future__ import annotations

import math
from shared_data import OBSSharedData


def fmt_age(value: float) -> str:
    return "--" if not math.isfinite(value) else f"{value:.3f} s"


def main() -> int:
    shared = OBSSharedData()
    try:
        health = shared.read_acquisition_health()
        print("OBS Acquisition Manager")
        print("-----------------------")
        print(f"PID            : {health.process_id}")
        print(f"Core           : {'LIVE' if health.is_alive() else 'STALE'}")
        print(f"Heartbeat age  : {fmt_age(health.heartbeat_age_s())}")
        print(f"TCP 54300      : {'CONNECTED' if health.command_connected else 'DISCONNECTED'}")
        print(f"TCP 54301      : {'CONNECTED' if health.data_connected else 'DISCONNECTED'}")
        print(f"IPC broker     : {'RUNNING' if health.broker_running else 'STOPPED'}")
        for source in ("command", "data", "adc", "ahrs", "depth", "controller", "gnss", "usbl"):
            print(f"{source.upper():14}: {fmt_age(health.source_age_s(source))}")
    finally:
        shared.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
