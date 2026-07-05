#!/usr/bin/env python3
"""
LLM Router Metrics Aggregation Module.

Reads JSONL router logs and computes aggregated metrics per the
LLM Router Dashboard Design Specification.

Exposes:
  get_router_metrics(log_path, window) -> dict
  The returned dict matches the /api/router-metrics JSON schema (§5.2).
"""
import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Default log path ────────────────────────────────────────────────
DEFAULT_LOG_PATH = Path(os.path.expanduser("~/.hermes/router/logs.jsonl"))


# ── Window helpers ──────────────────────────────────────────────────

def _window_bounds(window: str, now: datetime = None):
    """Return (start, end, label) for a time window."""
    if now is None:
        now = datetime.now(timezone.utc)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if window == "today":
        return today_start, now, "Today"
    elif window == "7d":
        return today_start - timedelta(days=7), now, "Last 7 days"
    elif window == "30d":
        return today_start - timedelta(days=30), now, "Last 30 days"
    else:
        raise ValueError(f"Invalid window parameter: {window}")


# ── Log reader ──────────────────────────────────────────────────────

def _read_logs(log_path: Path, window_start, window_end):
    """Read JSONL logs and return entries that fall within the window."""
    entries = []
    if not log_path.exists():
        return entries

    with open(log_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Parse timestamp
            ts_str = entry.get("timestamp", "")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            # Filter by window
            if window_start <= ts <= window_end:
                entries.append(entry)

    return entries


# ── Aggregation ─────────────────────────────────────────────────────

def _percentile(data, p):
    """Compute the p-th percentile (e.g., p=50 for median, p=95)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = k - f
    if f + 1 < len(sorted_data):
        return sorted_data[f] + c * (sorted_data[f + 1] - sorted_data[f])
    return float(sorted_data[f])


def compute_metrics(entries):
    """Aggregate metrics from a list of router log entries."""
    total = len(entries)

    if total == 0:
        return _empty_metrics()

    # ── 2.1 Request counts ──
    local_reqs = [e for e in entries if e.get("route") == "local"]
    remote_reqs = [e for e in entries if e.get("route") == "remote"]
    local_count = len(local_reqs)
    remote_count = len(remote_reqs)
    local_pct = (local_count / total) * 100 if total > 0 else 0.0

    # ── 2.2 Cost tracking ──
    # cost_saved: sum of estimated_remote_cost_usd for local requests
    # (what we WOULD have paid if routed remotely — cost we actually paid local = $0)
    cost_saved = sum(
        e.get("estimated_remote_cost_usd", 0.0)
        for e in local_reqs
    )
    # remote_spend: sum of actual cost_usd for remote requests
    remote_spend = sum(e.get("cost_usd", 0.0) for e in remote_reqs)
    # Also include fallback costs in remote spend (fallbacks are local-then-remote)
    fallback_reqs = [e for e in entries if e.get("status") == "fallback"]
    remote_spend += sum(e.get("cost_usd", 0.0) for e in fallback_reqs)

    savings_rate = cost_saved / local_count if local_count > 0 else 0.0
    net_savings = cost_saved - remote_spend

    # ── 2.3 Failure & Reliability ──
    failed_count = sum(1 for e in entries if e.get("status") == "failed")
    fallback_count = len(fallback_reqs)
    failure_rate = (failed_count / total) * 100 if total > 0 else 0.0
    fallback_rate = (fallback_count / local_count) * 100 if local_count > 0 else 0.0
    success_rate = 100.0 - failure_rate

    # ── 2.4 Latency ──
    all_times = [e.get("response_time_ms", 0) for e in entries]
    local_times = [e.get("response_time_ms", 0) for e in local_reqs if e.get("status") != "failed"]
    remote_times = [e.get("response_time_ms", 0) for e in remote_reqs if e.get("status") != "failed"]

    # Include fallback times in local? They started local. Per spec, local avg
    # should include local attempts. But fallbacks ultimately go remote, so
    # the spec doesn't strictly define. We'll compute local latency from all
    # local-route entries (excluding failed/timeout distortions), and
    # remote latency from remote-route successes.
    avg_latency = sum(all_times) / total if total > 0 else 0.0
    p50_latency = _percentile(all_times, 50)
    p95_latency = _percentile(all_times, 95)
    local_avg = sum(local_times) / len(local_times) if local_times else 0.0
    remote_avg = sum(remote_times) / len(remote_times) if remote_times else 0.0

    # ── 2.5 Model distribution ──
    model_map = {}
    for e in entries:
        model = e.get("model", "unknown")
        if model not in model_map:
            model_map[model] = {
                "count": 0, "local_count": 0, "remote_count": 0,
                "total_tokens": 0, "latencies": [],
            }
        mm = model_map[model]
        mm["count"] += 1
        if e.get("route") == "local":
            mm["local_count"] += 1
        else:
            mm["remote_count"] += 1
        mm["total_tokens"] += e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
        mm["latencies"].append(e.get("response_time_ms", 0))

    models_list = []
    for name, mm in model_map.items():
        models_list.append({
            "model": name,
            "count": mm["count"],
            "local_count": mm["local_count"],
            "remote_count": mm["remote_count"],
            "total_tokens": mm["total_tokens"],
            "avg_latency_ms": round(sum(mm["latencies"]) / len(mm["latencies"]), 2)
            if mm["latencies"] else 0.0,
        })
    models_list.sort(key=lambda m: m["count"], reverse=True)

    # ── 2.6 PII Redaction ──
    pii_detections = sum(1 for e in entries if e.get("pii_detected", False))
    pii_redactions = sum(1 for e in entries if e.get("pii_redacted", False))
    pii_redaction_rate = (pii_redactions / pii_detections) * 100 if pii_detections > 0 else 0.0

    # ── 2.7 Routing reasons ──
    reason_map = {}
    for e in entries:
        reason = e.get("routing_reason", "unknown")
        route = e.get("route", "unknown")
        if reason not in reason_map:
            reason_map[reason] = {"count": 0, "route": route}  # last route wins for simplicity
        reason_map[reason]["count"] += 1

    reasons_list = sorted(
        [{"reason": r, "count": v["count"], "route": v["route"]}
         for r, v in reason_map.items()],
        key=lambda x: x["count"], reverse=True
    )[:10]

    # ── Build response ──
    now = datetime.now(timezone.utc)
    window_start, window_end, label = _window_bounds("today", now)

    return {
        "ok": True,
        "window": {
            "label": label,
            "start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "summary": {
            "total_requests": total,
            "local_requests": local_count,
            "remote_requests": remote_count,
            "local_pct": round(local_pct, 2),
            "cost_saved": round(cost_saved, 6),
            "remote_spend": round(remote_spend, 6),
            "savings_rate": round(savings_rate, 6),
            "net_savings": round(net_savings, 6),
            "failed_requests": failed_count,
            "failure_rate": round(failure_rate, 2),
            "fallback_count": fallback_count,
            "fallback_rate": round(fallback_rate, 2),
            "success_rate": round(success_rate, 2),
            "avg_latency_ms": round(avg_latency, 2),
            "p50_latency_ms": round(p50_latency, 2),
            "p95_latency_ms": round(p95_latency, 2),
            "local_avg_latency_ms": round(local_avg, 2),
            "remote_avg_latency_ms": round(remote_avg, 2),
            "pii_detections": pii_detections,
            "pii_redactions": pii_redactions,
            "pii_redaction_rate": round(pii_redaction_rate, 2),
        },
        "models": models_list,
        "routing_reasons": reasons_list,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _empty_metrics():
    """Return metrics when no requests exist in the window."""
    now = datetime.now(timezone.utc)
    window_start, window_end, label = _window_bounds("today", now)
    return {
        "ok": True,
        "window": {
            "label": label,
            "start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "summary": {
            "total_requests": 0,
            "local_requests": 0,
            "remote_requests": 0,
            "local_pct": 0.0,
            "cost_saved": 0.0,
            "remote_spend": 0.0,
            "savings_rate": 0.0,
            "net_savings": 0.0,
            "failed_requests": 0,
            "failure_rate": 0.0,
            "fallback_count": 0,
            "fallback_rate": 0.0,
            "success_rate": 100.0,
            "avg_latency_ms": 0.0,
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "local_avg_latency_ms": 0.0,
            "remote_avg_latency_ms": 0.0,
            "pii_detections": 0,
            "pii_redactions": 0,
            "pii_redaction_rate": 0.0,
        },
        "models": [],
        "routing_reasons": [],
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def get_router_metrics(log_path=None, window="today"):
    """
    Compute aggregated router metrics for the given time window.

    Args:
        log_path: Path to JSONL log file (default: ~/.hermes/router/logs.jsonl)
        window: "today", "7d", or "30d"

    Returns:
        dict matching the /api/router-metrics JSON response schema.
    """
    if log_path is None:
        log_path = DEFAULT_LOG_PATH

    log_path = Path(log_path)

    now = datetime.now(timezone.utc)
    window_start, window_end, label = _window_bounds(window, now)

    entries = _read_logs(log_path, window_start, window_end)
    metrics = compute_metrics(entries)

    # Override window metadata with actual requested window
    metrics["window"] = {
        "label": label,
        "start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    return metrics


# ── CLI entry point for testing ─────────────────────────────────────

if __name__ == "__main__":
    import sys
    window = sys.argv[1] if len(sys.argv) > 1 else "today"
    result = get_router_metrics(window=window)
    print(json.dumps(result, indent=2))
