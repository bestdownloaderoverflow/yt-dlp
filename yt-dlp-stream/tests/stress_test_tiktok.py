#!/usr/bin/env python3
"""Stress test for /tiktok endpoint with 100-200 concurrent requests."""

import asyncio
import aiohttp
import time
import json
import statistics
from datetime import datetime

GATEWAY_URL = "http://localhost:9111"
TIKTOK_URL = "https://www.tiktok.com/@louie.rodrigo3/video/7598105227488693524?is_from_webapp=1&sender_device=pc"
CONCURRENCY_MIN = 100
CONCURRENCY_MAX = 200
DURATION_SECONDS = 180

results = {
    "total": 0,
    "success": 0,
    "failed": 0,
    "invalid": 0,
    "status_codes": {},
    "latencies": [],
    "errors": [],
    "extract_sources": {},
    "response_statuses": {},
}
lock = asyncio.Lock()

def validate_tiktok_payload(data):
    if not isinstance(data, dict):
        return False, f"non_object={type(data).__name__}"
    required = ["status", "title", "author", "statistics", "download_link"]
    missing = [key for key in required if key not in data]
    if missing:
        return False, f"missing={missing}"
    if data.get("status") not in {"tunnel", "picker"}:
        return False, f"unexpected_status={data.get('status')!r}"
    if not isinstance(data.get("download_link"), dict) or not data["download_link"]:
        return False, "empty_download_link"
    return True, ""

async def fetch_tiktok(session):
    start = time.monotonic()
    try:
        async with session.post(
            f"{GATEWAY_URL}/tiktok",
            json={"url": TIKTOK_URL, "force_ipv6": False},
            timeout=aiohttp.ClientTimeout(total=None),
        ) as resp:
            latency = time.monotonic() - start
            body = await resp.text()
            async with lock:
                results["total"] += 1
                results["latencies"].append(latency)
                code = resp.status
                results["status_codes"][code] = results["status_codes"].get(code, 0) + 1
                if resp.status == 200:
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as e:
                        results["failed"] += 1
                        results["invalid"] += 1
                        results["errors"].append(f"invalid_json={e} body={body[:200]}")
                        return

                    ok, reason = validate_tiktok_payload(data)
                    if ok:
                        results["success"] += 1
                        source = data.get("extract_source", "unknown")
                        status = data.get("status", "unknown")
                        results["extract_sources"][source] = results["extract_sources"].get(source, 0) + 1
                        results["response_statuses"][status] = results["response_statuses"].get(status, 0) + 1
                        print(f"[SUCCESS] status={status} source={source} title={data.get('title', '')[:50]}", flush=True)
                    else:
                        results["failed"] += 1
                        results["invalid"] += 1
                        results["errors"].append(f"invalid_payload={reason} body={body[:200]}")
                        print(f"[INVALID PAYLOAD] {reason}", flush=True)
                else:
                    results["failed"] += 1
                    results["errors"].append(f"status={code} body={body[:200]}")
                    print(f"[ERROR] status={code} body={body[:200]}", flush=True)
    except Exception as e:
        latency = time.monotonic() - start
        async with lock:
            results["total"] += 1
            results["failed"] += 1
            results["latencies"].append(latency)
            results["errors"].append(f"exception={type(e).__name__}: {e!r}")
            print(f"[EXCEPTION] {type(e).__name__}: {e!r}", flush=True)

def target_concurrency(start, duration):
    elapsed = time.monotonic() - start
    ratio = min(1.0, max(0.0, elapsed / max(1, duration)))
    return int(CONCURRENCY_MIN + (CONCURRENCY_MAX - CONCURRENCY_MIN) * ratio)

async def run_stress(session, duration):
    start = time.monotonic()
    end_time = start + duration
    tasks = []
    while time.monotonic() < end_time:
        tasks = [task for task in tasks if not task.done()]
        target = target_concurrency(start, duration)
        while len(tasks) < target:
            tasks.append(asyncio.create_task(fetch_tiktok(session)))
        await asyncio.sleep(0.1)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def progress_reporter(duration):
    start = time.monotonic()
    while time.monotonic() - start < duration:
        elapsed = time.monotonic() - start
        async with lock:
            rps = results["total"] / max(1, elapsed)
            print(f"[{elapsed:.0f}s/{duration}s] total={results['total']} ok={results['success']} fail={results['failed']} invalid={results['invalid']} rps={rps:.1f}", flush=True)
        await asyncio.sleep(10)

async def main():
    print(f"=== TikTok Stress Test ===")
    print(f"Gateway: {GATEWAY_URL}")
    print(f"URL: {TIKTOK_URL}")
    print(f"Concurrency: {CONCURRENCY_MIN}-{CONCURRENCY_MAX}")
    print(f"Duration: {DURATION_SECONDS}s")
    print(f"Start: {datetime.now().isoformat()}")
    print()

    connector = aiohttp.TCPConnector(limit=0, limit_per_host=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        reporter = asyncio.create_task(progress_reporter(DURATION_SECONDS))
        await run_stress(session, DURATION_SECONDS)
        reporter.cancel()

    print()
    print(f"=== Results ===")
    print(f"Total requests: {results['total']}")
    print(f"Success: {results['success']}")
    print(f"Failed: {results['failed']}")
    print(f"Invalid payloads: {results['invalid']}")
    if results["latencies"]:
        print(f"Avg latency: {statistics.mean(results['latencies']):.2f}s")
        print(f"P50 latency: {statistics.median(results['latencies']):.2f}s")
        sorted_lat = sorted(results["latencies"])
        p95_idx = int(len(sorted_lat) * 0.95)
        print(f"P95 latency: {sorted_lat[p95_idx]:.2f}s")
    print(f"RPS: {results['total'] / DURATION_SECONDS:.1f}")
    print(f"Status codes: {results['status_codes']}")
    print(f"Extract sources: {results['extract_sources']}")
    print(f"Response statuses: {results['response_statuses']}")
    if results["errors"][:5]:
        print(f"Sample errors:")
        for err in results["errors"][:5]:
            print(f"  - {err}")

if __name__ == "__main__":
    asyncio.run(main())
