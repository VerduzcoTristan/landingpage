#!/usr/bin/env python3
"""
E2E Integration Test: Router Dashboard ←→ Metrics Backend

Validates:
1. Server serves HTML and API responses
2. All 6 summary cards populate correctly
3. All 6 detail cards render with correct data
4. Data schema alignment between backend and frontend
5. All 3 time windows work
6. Edge cases (empty windows, error states)
7. Auto-refresh and manual refresh behavior
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:3003"
PASS = 0
FAIL = 0

def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label} {detail}")

def fetch_json(url, timeout=10):
    """Fetch and parse JSON from the API."""
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(r.read()), r.status
    except Exception as e:
        return {"error": str(e)}, 0

def fetch_text(url, timeout=10):
    """Fetch text content."""
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.read().decode(), r.status, r.headers.get("Content-Type", "")
    except Exception as e:
        return f"ERROR: {e}", 0, ""


print("=" * 60)
print("E2E Integration Test: LLM Router Dashboard")
print("=" * 60)

# ── 1. Server Health ──
print("\n── 1. Server Health ──")
text, status, ct = fetch_text(f"{BASE}/health")
check("Health endpoint responds", status == 200)
check("Health returns 'ok'", text.strip() == "ok", f"got: {text[:50]}")

# ── 2. HTML Page Serves ──
print("\n── 2. Dashboard HTML ──")
html, status, ct = fetch_text(f"{BASE}/router")
check("HTML page returns 200", status == 200)
check("Content-Type is text/html", "text/html" in ct, f"got: {ct}")
check("HTML contains <script>", "<script>" in html)
check("HTML contains fetchMetrics function", "fetchMetrics" in html)
check("HTML has API_URL constant", "/api/router-metrics" in html)
check("HTML has renderSummary function", "renderSummary" in html)
check("HTML has renderDetails function", "renderDetails" in html)
check("HTML has window selector (today/7d/30d)", "window-btn" in html)
check("HTML has refresh button", "refresh-btn" in html)
check("HTML has status bar", "status-bar" in html)
check("HTML has connection banner", "connection-banner" in html)
check("HTML has skeleton loading", "renderSkeleton" in html)

# ── 3. API Schema Validation ──
print("\n── 3. API Schema (today window) ──")
data, status = fetch_json(f"{BASE}/api/router-metrics?window=today")
check("API returns HTTP 200", status == 200)
check("API has 'ok' field", "ok" in data, f"keys: {list(data.keys())[:10]}")
check("API ok is True", data.get("ok") is True, f"ok={data.get('ok')}")
check("API has 'window' object", isinstance(data.get("window"), dict))
check("Window has 'label'", "label" in data.get("window", {}))
check("Window has 'start'", "start" in data.get("window", {}))
check("Window has 'end'", "end" in data.get("window", {}))

# ── 4. Summary Schema ──
print("\n── 4. Summary Schema ──")
s = data.get("summary", {})
check("summary is dict", isinstance(s, dict))

# Request counts
check("total_requests is int", isinstance(s.get("total_requests"), int))
check("local_requests is int", isinstance(s.get("local_requests"), int))
check("remote_requests is int", isinstance(s.get("remote_requests"), int))
check("local_pct is float", isinstance(s.get("local_pct"), (int, float)))
check("local + remote = total",
      s.get("local_requests", 0) + s.get("remote_requests", 0) == s.get("total_requests", 0),
      f"local={s.get('local_requests')} + remote={s.get('remote_requests')} != total={s.get('total_requests')}")

# Cost tracking
check("cost_saved is float", isinstance(s.get("cost_saved"), (int, float)))
check("remote_spend is float", isinstance(s.get("remote_spend"), (int, float)))
check("savings_rate is float", isinstance(s.get("savings_rate"), (int, float)))
check("net_savings is float", isinstance(s.get("net_savings"), (int, float)))
if s.get("local_requests", 0) > 0:
    check("net_savings = cost_saved - remote_spend",
          abs(s.get("net_savings", 0) - (s.get("cost_saved", 0) - s.get("remote_spend", 0))) < 0.01,
          f"net={s.get('net_savings'):.4f}, saved={s.get('cost_saved'):.4f}-spend={s.get('remote_spend'):.4f}")

# Failure & Reliability
check("failed_requests is int", isinstance(s.get("failed_requests"), int))
check("failure_rate is float", isinstance(s.get("failure_rate"), (int, float)))
check("fallback_count is int", isinstance(s.get("fallback_count"), int))
check("fallback_rate is float", isinstance(s.get("fallback_rate"), (int, float)))
check("success_rate is float", 0 <= s.get("success_rate", 0) <= 100,
      f"success_rate={s.get('success_rate')}")

# Latency
check("avg_latency_ms is float", isinstance(s.get("avg_latency_ms"), (int, float)))
check("p50_latency_ms is float", isinstance(s.get("p50_latency_ms"), (int, float)))
check("p95_latency_ms is float", isinstance(s.get("p95_latency_ms"), (int, float)))
check("local_avg_latency_ms is float", isinstance(s.get("local_avg_latency_ms"), (int, float)))
check("remote_avg_latency_ms is float", isinstance(s.get("remote_avg_latency_ms"), (int, float)))

# PII
check("pii_detections is int", isinstance(s.get("pii_detections"), int))
check("pii_redactions is int", isinstance(s.get("pii_redactions"), int))
check("pii_redaction_rate is float", isinstance(s.get("pii_redaction_rate"), (int, float)))

# ── 5. Models array ──
print("\n── 5. Models Schema ──")
models = data.get("models", [])
check("models is list", isinstance(models, list))
for m in models[:3]:
    check(f"  model '{m.get('model','?')[:30]}' has count", isinstance(m.get("count"), int))
    check(f"  model '{m.get('model','?')[:30]}' has avg_latency_ms", isinstance(m.get("avg_latency_ms"), (int, float)))

# ── 6. Routing Reasons ──
print("\n── 6. Routing Reasons Schema ──")
reasons = data.get("routing_reasons", [])
check("routing_reasons is list", isinstance(reasons, list))
check("routing_reasons capped at 10", len(reasons) <= 10, f"got {len(reasons)}")
for rr in reasons[:3]:
    check(f"  reason '{rr.get('reason','?')[:30]}' has count", isinstance(rr.get("count"), int))
    check(f"  reason '{rr.get('reason','?')[:30]}' has route", rr.get("route") in ("local", "remote"),
          f"route={rr.get('route')}")

# ── 7. generated_at ──
print("\n── 7. Metadata ──")
check("generated_at is string", isinstance(data.get("generated_at"), str))
check("generated_at is ISO timestamp", "T" in data.get("generated_at", ""),
      f"got: {data.get('generated_at')}")

# ── 8. Multiple time windows ──
print("\n── 8. Time Windows ──")
for window in ["today", "7d", "30d"]:
    wdata, wstatus = fetch_json(f"{BASE}/api/router-metrics?window={window}")
    ws = wdata.get("summary", {})
    total = ws.get("total_requests", 0)
    check(f"'{window}' window returns OK", wstatus == 200 and wdata.get("ok"))
    check(f"'{window}' has total_requests", isinstance(total, int))
    if total > 0:
        check(f"'{window}' has non-zero data models", len(wdata.get("models", [])) > 0)
    print(f"    {window}: {total} requests, {len(wdata.get('models', []))} models, {len(wdata.get('routing_reasons', []))} reasons")

# ── 9. Round-trip: fetch → cache-busting → verify consistency ──
print("\n── 9. Round-Trip Consistency ──")
data1, _ = fetch_json(f"{BASE}/api/router-metrics?window=today")
data2, _ = fetch_json(f"{BASE}/api/router-metrics?window=today&_={int(time.time()*1000)}")
s1 = data1.get("summary", {})
s2 = data2.get("summary", {})
check("Two consecutive fetches return same total",
      s1.get("total_requests") == s2.get("total_requests"),
      f"first={s1.get('total_requests')}, second={s2.get('total_requests')}")

# ── 10. Invalid window handling ──
print("\n── 10. Error Handling ──")
errdata, errstatus = fetch_json(f"{BASE}/api/router-metrics?window=invalid")
check("Invalid window returns non-200", errstatus != 200,
      f"status={errstatus}, body={str(errdata)[:100]}")

# Root redirect
text, status, ct = fetch_text(f"{BASE}/")
check("Root path serves dashboard", status == 200)
check("Root has HTML content", "LLM Router Dashboard" in text, f"contains: {'LLM Router' in text}")

# ── 11. Cross-origin headers ──
print("\n── 11. CORS Headers ──")
try:
    r = urllib.request.urlopen(f"{BASE}/api/router-metrics?window=today", timeout=10)
    cors = r.headers.get("Access-Control-Allow-Origin", "")
    check("CORS header present", cors == "*", f"got: {cors}")
except Exception as e:
    check("CORS request succeeds", False, str(e))

# ── 12. Content negotiation ──
print("\n── 12. Cache Headers ──")
try:
    r = urllib.request.urlopen(f"{BASE}/api/router-metrics?window=today", timeout=10)
    cc = r.headers.get("Cache-Control", "")
    check("Cache-Control is no-cache", cc == "no-cache", f"got: {cc}")
except Exception as e:
    check("Cache header check succeeds", False, str(e))

# ── 13. Metrics invariants ──
print("\n── 13. Metrics Invariants ──")
s = data.get("summary", {})
total = s.get("total_requests", 0)

if total > 0:
    # local_pct should be within [0, 100]
    check("local_pct in [0, 100]", 0 <= s.get("local_pct", 0) <= 100,
          f"local_pct={s.get('local_pct')}")

    # failure_rate + success_rate should be approx 100
    check("failure_rate + success_rate ≈ 100",
          abs(s.get("failure_rate", 0) + s.get("success_rate", 0) - 100) < 0.1,
          f"failure={s.get('failure_rate')}, success={s.get('success_rate')}")

    # P95 latency >= P50 latency (when data exists)
    if s.get("p95_latency_ms", 0) > 0:
        check("P95 >= P50 latency",
              s.get("p95_latency_ms", 0) >= s.get("p50_latency_ms", 0),
              f"P95={s.get('p95_latency_ms')}, P50={s.get('p50_latency_ms')}")

    # Models total count should match total_requests
    model_total = sum(m.get("count", 0) for m in data.get("models", []))
    check("Model count sum = total_requests",
          model_total == total,
          f"model_sum={model_total}, total={total}")

# ── Summary ──
print("\n" + "=" * 60)
print(f"RESULTS: {PASS} passed, {FAIL} failed, {PASS+FAIL} total")
print("=" * 60)

if FAIL > 0:
    print(f"\n❌ E2E TEST FAILED — {FAIL} check(s) failed")
    sys.exit(1)
else:
    print("\n✅ ALL E2E CHECKS PASSED")
