"""Phase 1 smoke test: real end-to-end transactions through the gateway.

Sends a fixed request plan, records each response (status, latency, x-trace-id),
then checks the resulting database state directly in Postgres. Writes a JSON
report; the trace IDs it records are what validate_telemetry.py looks up.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Orders that must succeed, as (item_id, quantity); stock is seeded in testbed/postgres/init.sql.
ORDERS = [("sku-001", 2), ("sku-002", 1), ("sku-003", 3), ("sku-005", 10), ("sku-001", 1)]
EXPECTED_ERROR = ("sku-004", 999)  # more than the seeded stock: business-rule rejection, 409


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def call(client: httpx.Client, label, method, path, route, body, expected, expected_error=False) -> dict:
    t0_ns = time.time_ns()
    response = client.request(method, path, json=body)
    t1_ns = time.time_ns()
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return {
        "label": label,
        "method": method,
        "path": path,
        "route": route,
        "request_body": body,
        "status_code": response.status_code,
        "expected_status": expected,
        "expected_error": expected_error,
        "ok": response.status_code == expected,
        "trace_id": response.headers.get("x-trace-id"),
        "latency_ms": round((t1_ns - t0_ns) / 1e6, 3),
        "sent_unix_nano": t0_ns,
        "received_unix_nano": t1_ns,
        "response": payload,
    }


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "shop", "-d", "shop", "-At", "-F", "|", "-c", sql],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line.split("|") for line in out.splitlines() if line]


def check(checks: list, check_id: str, description: str, passed: bool, evidence) -> None:
    checks.append({"id": check_id, "description": description, "status": "pass" if passed else "fail",
                   "evidence": evidence})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    started = now_iso()
    requests: list[dict] = []
    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        items = sorted({item for item, _ in ORDERS} | {EXPECTED_ERROR[0]})
        initial = {}
        for item in items:
            r = call(client, f"stock-before:{item}", "GET", f"/api/stock/{item}", "/api/stock/{item_id}", None, 200)
            requests.append(r)
            initial[item] = r["response"]["stock"] if r["ok"] else None

        for item, qty in ORDERS:
            requests.append(call(client, f"order:{item}x{qty}", "POST", "/api/orders", "/api/orders",
                                 {"item_id": item, "quantity": qty}, 201))

        first = next(r for r in requests if r["label"].startswith("order:"))
        if first["ok"]:
            order_id = first["response"]["order_id"]
            requests.append(call(client, "order-lookup", "GET", f"/api/orders/{order_id}",
                                 "/api/orders/{order_id}", None, 200))

        item, qty = EXPECTED_ERROR
        requests.append(call(client, f"expected-reject:{item}x{qty}", "POST", "/api/orders", "/api/orders",
                             {"item_id": item, "quantity": qty}, 409, expected_error=True))
    finished = now_iso()

    checks: list[dict] = []
    check(checks, "S1", "Every request returned its expected status code",
          all(r["ok"] for r in requests),
          [{"label": r["label"], "status": r["status_code"], "expected": r["expected_status"]} for r in requests])

    orders = [r for r in requests if r["label"].startswith("order:")]
    check(checks, "S2", "Every successful order is confirmed with a payment id",
          all(r["ok"] and r["response"].get("status") == "confirmed" and r["response"].get("payment_id")
              for r in orders),
          [{"order_id": (r["response"] or {}).get("order_id"), "status": (r["response"] or {}).get("status")}
           for r in orders])

    check(checks, "S3", "Every response carries a W3C-format trace id (x-trace-id header)",
          all(r["trace_id"] and len(r["trace_id"]) == 32 and int(r["trace_id"], 16) != 0 for r in requests),
          [r["trace_id"] for r in requests])

    check(checks, "S4", "No 5xx responses", not any(r["status_code"] >= 500 for r in requests),
          {"5xx_count": sum(r["status_code"] >= 500 for r in requests)})

    # Database state, read directly from Postgres rather than through the services.
    ids = [r["response"]["order_id"] for r in orders if r["ok"]]
    id_list = ",".join(f"'{i}'" for i in ids)
    db_orders = psql(f"SELECT order_id, status, quantity, amount_cents, payment_id FROM orders "
                     f"WHERE order_id IN ({id_list}) ORDER BY created_at") if ids else []
    responses = {r["response"]["order_id"]: r["response"] for r in orders if r["ok"]}
    db_ok = len(db_orders) == len(ORDERS) and all(
        row[1] == "confirmed" and row[4] == responses[row[0]]["payment_id"]
        and int(row[3]) == responses[row[0]]["amount_cents"] for row in db_orders)
    check(checks, "S5", "Orders persisted in Postgres as confirmed, matching the API responses", db_ok,
          {"rows": [{"order_id": r[0], "status": r[1], "quantity": int(r[2]), "amount_cents": int(r[3]),
                     "payment_id": r[4]} for r in db_orders]})

    stock_rows = {r[0]: int(r[1]) for r in psql("SELECT item_id, stock FROM items ORDER BY item_id")}
    expected_stock = dict(initial)
    for item, qty in ORDERS:
        expected_stock[item] -= qty
    check(checks, "S6", "Item stock in Postgres decreased by exactly the ordered quantities "
                        "(the rejected order changed nothing)",
          all(stock_rows.get(i) == expected_stock[i] for i in items),
          {"initial": initial, "expected": expected_stock, "observed": {i: stock_rows.get(i) for i in items}})

    reservations = psql(f"SELECT count(*) FROM reservations WHERE order_id IN ({id_list})") if ids else [["0"]]
    check(checks, "S7", "One stock reservation per confirmed order", int(reservations[0][0]) == len(ids),
          {"reservations": int(reservations[0][0]), "orders": len(ids)})

    report = {
        "schema": "evidencerca.phase1.smoke_test/v1",
        "run_id": args.run_id,
        "base_url": args.base_url,
        "started_at": started,
        "finished_at": finished,
        "request_count": len(requests),
        "expected_error_requests": sum(r["expected_error"] for r in requests),
        "requests": requests,
        "checks": checks,
        "passed": all(c["status"] == "pass" for c in checks),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    for c in checks:
        print(f"[{c['status'].upper()}] {c['id']} {c['description']}")
    print(f"smoke test {'PASSED' if report['passed'] else 'FAILED'}: {len(requests)} requests -> {args.out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
