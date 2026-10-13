"""Seeded open-loop workload generator + TCP reachability probes (runs inside the VM).

Runs in the `workload` compose container (uninstrumented, excluded from
docker_stats). The whole request schedule (Poisson arrivals, request kinds and
parameters) is generated from the seed *before* the first request, so two runs
with the same seed offer an identical schedule (`schedule_sha256`). Requests are
sent open-loop; if `--concurrency` requests are already in flight, the arrival
is shed and recorded rather than delayed.

All timestamps are this container's clock, i.e. the Colima VM clock.

Outputs (in --out):
  start.json        written as soon as the clock starts (t0)
  requests.jsonl    one line per scheduled arrival (sent or shed)
  probes.jsonl      TCP connect time to each service every --probe-interval s
  summary.json      offered/achieved throughput, status counts, latency quantiles
"""

import argparse
import asyncio
import hashlib
import json
import os
import random
import socket
import time

import httpx

ITEMS = ["sku-001", "sku-002", "sku-003", "sku-005"]
# kind -> share of arrivals. Paths: stock = gateway->inventory; order = gateway->inventory, gateway->order->
# {inventory, payment}; lookup = gateway->order; invalid = rejected at gateway/inventory (expected 4xx).
MIX = [("stock", 0.35), ("order", 0.45), ("lookup", 0.10), ("invalid", 0.10)]
EXPECTED = {"stock": 200, "order": 201, "lookup": 200,
            "invalid_unknown_item": 404, "invalid_insufficient_stock": 409, "invalid_quantity": 422}
PROBE_TARGETS = ["gateway", "order", "payment", "inventory"]


def build_schedule(seed: int, rate: float, duration_s: float) -> list[dict]:
    rng = random.Random(seed)
    schedule, t = [], 0.0
    while True:
        t += rng.expovariate(rate)
        if t >= duration_s:
            return schedule
        r, acc, kind = rng.random(), 0.0, MIX[-1][0]
        for name, share in MIX:
            acc += share
            if r < acc:
                kind = name
                break
        spec = {"i": len(schedule), "offset_s": round(t, 6)}
        if kind == "stock":
            spec.update(kind="stock", item=rng.choice(ITEMS))
        elif kind == "order":
            spec.update(kind="order", item=rng.choice(ITEMS), quantity=rng.randint(1, 3))
        elif kind == "lookup":
            spec.update(kind="lookup", pick=rng.randrange(1 << 30))
        else:
            sub = rng.choice(["unknown_item", "insufficient_stock", "quantity"])
            spec.update(kind=f"invalid_{sub}")
        schedule.append(spec)


def request_for(spec: dict, created: list[str]) -> tuple[str, str, dict | None, str]:
    """(method, path, json, effective kind). Lookups fall back to a stock read until an order exists."""
    kind = spec["kind"]
    if kind == "stock":
        return "GET", f"/api/stock/{spec['item']}", None, kind
    if kind == "order":
        return "POST", "/api/orders", {"item_id": spec["item"], "quantity": spec["quantity"]}, kind
    if kind == "lookup":
        if created:
            return "GET", f"/api/orders/{created[spec['pick'] % len(created)]}", None, kind
        return "GET", f"/api/stock/{ITEMS[spec['pick'] % len(ITEMS)]}", None, "stock"
    if kind == "invalid_unknown_item":
        return "POST", "/api/orders", {"item_id": "sku-404", "quantity": 1}, kind
    if kind == "invalid_insufficient_stock":
        return "POST", "/api/orders", {"item_id": "sku-004", "quantity": 999}, kind
    return "POST", "/api/orders", {"item_id": "sku-001", "quantity": 0}, kind


def quantile(values: list[float], q: float):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q * len(s)))], 3)


async def probe_loop(out, interval_s: float, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def connect(host: str) -> dict:
        t0 = time.time_ns()
        try:
            with socket.create_connection((host, 8000), timeout=5.0):
                pass
            return {"target": host, "t_ns": t0, "ok": True, "connect_ms": round((time.time_ns() - t0) / 1e6, 3)}
        except OSError as exc:
            return {"target": host, "t_ns": t0, "ok": False, "error": type(exc).__name__,
                    "connect_ms": round((time.time_ns() - t0) / 1e6, 3)}

    while not stop.is_set():
        results = await asyncio.gather(*(loop.run_in_executor(None, connect, h) for h in PROBE_TARGETS))
        for r in results:
            out.write(json.dumps(r) + "\n")
        out.flush()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


async def run(args) -> None:
    schedule = build_schedule(args.seed, args.rate, args.duration)
    schedule_sha = hashlib.sha256(json.dumps(schedule, sort_keys=True).encode()).hexdigest()
    os.makedirs(args.out, exist_ok=True)
    t0 = time.time_ns()
    with open(os.path.join(args.out, "start.json"), "w") as f:
        json.dump({"t0_ns": t0, "seed": args.seed, "rate": args.rate, "duration_s": args.duration,
                   "concurrency": args.concurrency, "scheduled": len(schedule),
                   "schedule_sha256": schedule_sha}, f)

    created: list[str] = []
    records: list[dict] = []
    inflight = 0
    req_out = open(os.path.join(args.out, "requests.jsonl"), "w")
    probe_out = open(os.path.join(args.out, "probes.jsonl"), "w")
    stop = asyncio.Event()
    prober = asyncio.create_task(probe_loop(probe_out, args.probe_interval, stop))
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)

    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        async def send(spec: dict, sched_ns: int) -> None:
            nonlocal inflight
            method, path, body, kind = request_for(spec, created)
            rec = {"i": spec["i"], "kind": kind, "method": method, "path": path, "sched_ns": sched_ns,
                   "sent_ns": time.time_ns(), "expected": EXPECTED[kind]}
            try:
                resp = await client.request(method, path, json=body)
                rec.update(status=resp.status_code, trace_id=resp.headers.get("x-trace-id"))
                if kind == "order" and resp.status_code == 201:
                    created.append(resp.json()["order_id"])
            except httpx.HTTPError as exc:
                rec.update(status=None, error=type(exc).__name__, trace_id=None)
            finally:
                inflight -= 1
            rec["recv_ns"] = time.time_ns()
            rec["latency_ms"] = round((rec["recv_ns"] - rec["sent_ns"]) / 1e6, 3)
            rec["ok"] = rec["status"] == rec["expected"]
            records.append(rec)
            req_out.write(json.dumps(rec) + "\n")

        tasks = []
        for spec in schedule:
            sched_ns = t0 + int(spec["offset_s"] * 1e9)
            delay = (sched_ns - time.time_ns()) / 1e9
            if delay > 0:
                await asyncio.sleep(delay)
            if inflight >= args.concurrency:
                rec = {"i": spec["i"], "kind": spec["kind"], "sched_ns": sched_ns, "shed": True, "ok": False}
                records.append(rec)
                req_out.write(json.dumps(rec) + "\n")
                continue
            inflight += 1
            tasks.append(asyncio.create_task(send(spec, sched_ns)))
        await asyncio.gather(*tasks)

    stop.set()
    await prober
    req_out.close()
    probe_out.close()

    sent = [r for r in records if not r.get("shed")]
    end_ns = max((r["recv_ns"] for r in sent), default=time.time_ns())
    by_kind = {}
    for kind in sorted({r["kind"] for r in sent}):
        rs = [r for r in sent if r["kind"] == kind]
        lat = [r["latency_ms"] for r in rs if r.get("status") is not None]
        by_kind[kind] = {"n": len(rs), "ok": sum(r["ok"] for r in rs), "p50_ms": quantile(lat, 0.5),
                         "p95_ms": quantile(lat, 0.95)}
    statuses = {}
    for r in sent:
        key = str(r.get("status") or r.get("error"))
        statuses[key] = statuses.get(key, 0) + 1
    lat = [r["latency_ms"] for r in sent if r.get("status") is not None]
    summary = {
        "schedule_sha256": schedule_sha, "seed": args.seed, "t0_ns": t0, "end_ns": end_ns,
        "offered_rate_rps": args.rate, "scheduled": len(schedule), "sent": len(sent),
        "shed": len(records) - len(sent),
        "achieved_rate_rps": round(len(sent) / ((end_ns - t0) / 1e9), 3),
        "completed_ok": sum(r["ok"] for r in sent),
        "unexpected_outcome_rate": round(1 - sum(r["ok"] for r in sent) / max(1, len(sent)), 5),
        "status_counts": statuses, "p50_ms": quantile(lat, 0.5), "p95_ms": quantile(lat, 0.95),
        "by_kind": by_kind,
    }
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--rate", type=float, default=10.0, help="offered arrivals per second (Poisson)")
    p.add_argument("--duration", type=float, required=True, help="seconds of arrivals")
    p.add_argument("--concurrency", type=int, default=32, help="max in-flight requests; excess arrivals are shed")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--probe-interval", type=float, default=5.0)
    p.add_argument("--base-url", default="http://gateway:8000")
    p.add_argument("--out", default="/out")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
