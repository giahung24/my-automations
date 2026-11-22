#!/usr/bin/env python3
"""Query Google AI Studio usage (Cloud Monitoring) & billing/credits (BigQuery Export)."""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import google.auth.transport.requests
import requests
from google.oauth2 import service_account

KEY_PATH = "/home/jean/.config/gcp/hermes-monitor.json"
SCOPES = [
    "https://www.googleapis.com/auth/monitoring.read",
    "https://www.googleapis.com/auth/bigquery.readonly",
]
MON_BASE = "https://monitoring.googleapis.com/v3"
BQ_BASE = "https://bigquery.googleapis.com/bigquery/v2"


def get_session():
    creds = service_account.Credentials.from_service_account_file(KEY_PATH, scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {creds.token}"})
    return session, creds.project_id


def query_timeseries(session, project_id, metric_filter, start_time, end_time, alignment_seconds=86400):
    url = f"{MON_BASE}/projects/{project_id}/timeSeries"
    params = {
        "filter": metric_filter,
        "interval.startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": f"{alignment_seconds}s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
    }
    all_series = []
    while True:
        r = session.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        all_series.extend(data.get("timeSeries", []))
        token = data.get("nextPageToken")
        if not token:
            break
        params["pageToken"] = token
    return all_series


def check_bigquery_billing(session, project_id, days, dataset_id="billing_export"):
    tables_url = f"{BQ_BASE}/projects/{project_id}/datasets/{dataset_id}/tables"
    r = session.get(tables_url, timeout=15)
    if r.status_code != 200:
        print(f"\nBigQuery Billing ({dataset_id}): HTTP {r.status_code} - {r.text[:200]}")
        return
    tables = [
        t["tableReference"]["tableId"]
        for t in r.json().get("tables", [])
        if t["tableReference"]["tableId"].startswith("gcp_billing_export_")
    ]
    if not tables:
        print(f"\nBigQuery Billing ({dataset_id}): chưa có bảng dữ liệu (GCP thường mất vài giờ–24h sau khi bật để tạo bảng đầu tiên).")
        return

    table_id = sorted(tables)[-1]
    sql = f"""
    SELECT
      sku.description AS sku_desc,
      ROUND(SUM(cost), 4) AS gross_cost,
      ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 4) AS credits,
      ROUND(SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 4) AS net_cost,
      currency
    FROM `{project_id}.{dataset_id}.{table_id}`
    WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    GROUP BY sku_desc, currency
    ORDER BY gross_cost DESC
    """
    q_url = f"{BQ_BASE}/projects/{project_id}/queries"
    qr = session.post(q_url, json={"query": sql, "useLegacySql": False, "location": "EU"}, timeout=30)
    if qr.status_code != 200:
        print(f"\nBigQuery Query Error ({qr.status_code}): {qr.text[:300]}")
        return
    rows = qr.json().get("rows", [])
    print(f"\nBigQuery Billing ({dataset_id}.{table_id}, {days}d):")
    if not rows:
        print("  (Chưa có dòng chi phí nào trong khoảng thời gian này)")
        return
    total_gross = total_cred = total_net = 0.0
    currency = ""
    for row in rows:
        f = [c["v"] for c in row["f"]]
        sku, gross, cred, net, curr = f[0], float(f[1] or 0), float(f[2] or 0), float(f[3] or 0), f[4] or ""
        total_gross += gross
        total_cred += cred
        total_net += net
        currency = curr
        print(f"  - {sku}: gross={gross:.4f} {curr}, credits={cred:.4f}, net={net:.4f}")
    print(f"  TOTAL: gross={total_gross:.4f} {currency}, credits={total_cred:.4f}, net={total_net:.4f}")


def main():
    parser = argparse.ArgumentParser(description="Check Gemini API usage & billing")
    parser.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    args = parser.parse_args()

    session, project_id = get_session()
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=args.days)

    print(f"Project: {project_id}")
    print(f"Window:  {start_time.strftime('%Y-%m-%d %H:%M UTC')} -> {end_time.strftime('%Y-%m-%d %H:%M UTC')} ({args.days}d)\n")

    # 1. API Requests
    req_filter = (
        'metric.type = "serviceruntime.googleapis.com/api/request_count" '
        'AND resource.labels.service = "generativelanguage.googleapis.com"'
    )
    req_series = query_timeseries(session, project_id, req_filter, start_time, end_time)
    req_by_day = defaultdict(int)
    total_reqs = 0
    for ts in req_series:
        for pt in ts.get("points", []):
            val = int(pt["value"].get("int64Value", 0))
            req_by_day[pt["interval"]["endTime"][:10]] += val
            total_reqs += val

    # 2. Input Tokens (filtered to PerModelPerMinute to avoid double counting PerUser bucket)
    in_filter = (
        'metric.type = "generativelanguage.googleapis.com/quota/generate_content_paid_tier_input_token_count/usage" '
        'AND metric.labels.limit_name = "GenerateContentPaidTierInputTokensPerModelPerMinute"'
    )
    in_series = query_timeseries(session, project_id, in_filter, start_time, end_time)
    in_by_model = defaultdict(int)
    in_by_day = defaultdict(int)
    for ts in in_series:
        model = ts.get("metric", {}).get("labels", {}).get("model", "unknown")
        for pt in ts.get("points", []):
            val = int(pt["value"].get("int64Value", 0))
            in_by_model[model] += val
            in_by_day[pt["interval"]["endTime"][:10]] += val

    # 3. Output Tokens
    out_filter = 'metric.type = "generativelanguage.googleapis.com/generate_content_usage_output_token_count"'
    out_series = query_timeseries(session, project_id, out_filter, start_time, end_time)
    out_by_model = defaultdict(int)
    out_by_day = defaultdict(int)
    for ts in out_series:
        model = ts.get("metric", {}).get("labels", {}).get("model", "unknown")
        for pt in ts.get("points", []):
            val = int(pt["value"].get("int64Value", 0))
            out_by_model[model] += val
            out_by_day[pt["interval"]["endTime"][:10]] += val

    print(f"Total Requests:      {total_reqs:,}")
    print(f"Total Input Tokens:  {sum(in_by_model.values()):,}")
    print(f"Total Output Tokens: {sum(out_by_model.values()):,}\n")

    models = sorted(set(in_by_model) | set(out_by_model))
    print("By Model:")
    for m in models:
        print(f"  - {m}: {in_by_model[m]:,} in / {out_by_model[m]:,} out")

    days = sorted(set(req_by_day) | set(in_by_day) | set(out_by_day))
    print("\nBy Day:")
    for d in days:
        print(f"  - {d}: {req_by_day[d]:,} req · {in_by_day[d]:,} in · {out_by_day[d]:,} out")

    check_bigquery_billing(session, project_id, args.days)


if __name__ == "__main__":
    main()
