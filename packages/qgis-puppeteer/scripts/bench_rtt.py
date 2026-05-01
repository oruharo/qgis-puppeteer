"""Hub/Worker 経由の各コマンドの純粋 RTT を測定する。

MCP / Claude レイヤをバイパスして `AutomationClient` で直接 Hub に接続し、
`perf_counter` で各 `call()` の経過時間を測る。QGIS と Worker は事前に
立ち上げておく前提。
"""

from __future__ import annotations

import asyncio
import statistics
from time import perf_counter

from qgis_puppeteer.client import AutomationClient

HUB_URL = "ws://127.0.0.1:9876"


async def _timed(client: AutomationClient, label: str, command: str, params: dict) -> float:
    t0 = perf_counter()
    await client.call(command, params)
    dt = (perf_counter() - t0) * 1000.0
    print(f"  {label:40s} {dt:8.2f} ms")
    return dt


async def main() -> None:
    client = AutomationClient(url=HUB_URL)
    t0 = perf_counter()
    await client.connect()
    print(f"connect: {(perf_counter() - t0) * 1000:.2f} ms\n")

    instances = await client.list_instances()
    print(f"list_instances: {len(instances)} instance(s)")
    for i in instances:
        print(f"  {i.instance_id} pid={i.pid}")
    print()

    target = instances[0].instance_id if instances else None
    scenarios: list[tuple[str, str, dict]] = [
        ("qgis_get_canvas_extent", "qgis_get_canvas_extent", {}),
        ("qgis_list_layers", "qgis_list_layers", {}),
        (
            "qgis_select_features (id=1)",
            "qgis_select_features",
            {"layer_name": "buildings", "expression": "id = 1"},
        ),
        (
            "qgis_get_selected_features limit=2",
            "qgis_get_selected_features",
            {"layer_name": "buildings", "limit": 2},
        ),
        (
            "qgis_set_canvas_extent",
            "qgis_set_canvas_extent",
            {"xmin": 900, "ymin": -24800, "xmax": 1200, "ymax": -24600},
        ),
    ]

    # 各コマンド N 回実行して min/med/max を出す
    N = 5
    print(f"--- RTT per command (N={N}) ---")
    results: dict[str, list[float]] = {label: [] for label, *_ in scenarios}
    for _ in range(N):
        for label, command, params in scenarios:
            t0 = perf_counter()
            await client.call(command, params, instance=target)
            dt = (perf_counter() - t0) * 1000.0
            results[label].append(dt)

    print()
    print(f"{'command':45s} {'min':>8s} {'med':>8s} {'max':>8s}")
    for label, samples in results.items():
        print(
            f"{label:45s} {min(samples):8.2f} {statistics.median(samples):8.2f} {max(samples):8.2f}"
        )

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
