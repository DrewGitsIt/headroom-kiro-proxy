# Spec: Cloud Metrics Reporting

## Goal

Each kiro-proxy installation reports anonymous compression stats to a shared S3 bucket in the `ai-platform-dev` AWS account (111452723372, us-east-1). This gives the team a fleet-wide view of token savings without exposing conversation content.

## User Story

As a team lead, I want to see:
- How many people are using kiro-proxy
- Total messages proxied and % compressed
- Aggregate bytes and estimated dollars saved
- Whether compression is causing errors (fallthrough count)

## Architecture

```
kiro-proxy (local)
  → reporter.py (hourly, async)
    → boto3 (user's existing AWS creds via ai-platform-dev profile)
      → s3://kiro-proxy-metrics-111452723372/metrics/year=YYYY/month=MM/{install_id}/{date}.json

Athena (ai-platform-dev)
  → database: kiro_proxy
  → table: daily_metrics (partition projection: year, month)
  → query results: s3://kiro-proxy-athena-results-111452723372/
```

## Credential Strategy

Uses the user's existing AWS credential chain via `boto3.Session(profile_name="ai-platform-dev")`. Most engineers already have SSO access to this account. If credentials are missing or expired, the report silently fails and retries next hour.

No custom IAM keys, no credential distribution, no SigV4 reimplementation.

## Report Frequency

- **Interval:** Once per hour (3600s ± 600s random jitter)
- **Jitter:** Prevents thundering herd on the hour mark
- **Initial delay:** Random 1–5 minutes after proxy start (skip cold-start reports)

## Opt-Out

Telemetry is enabled by default. Users can opt out:

```bash
kiro-proxy telemetry off    # Disable
kiro-proxy telemetry on     # Re-enable
kiro-proxy telemetry status # Show state, install ID, last report date
```

The installer prompts during setup:
```
Enable anonymous compression metrics? (Y/n):
```

Config stored in `~/.kiro-proxy/config` as `telemetry=on|off`.

## Athena Schema

```sql
CREATE EXTERNAL TABLE kiro_proxy.daily_metrics (
  install_id STRING,
  proxy_version STRING,
  report_date STRING,
  requests_total INT,
  requests_compressed INT,
  bytes_saved BIGINT,
  est_tokens_saved INT,
  est_cost_saved_usd DOUBLE,
  avg_savings_pct DOUBLE,
  images_stripped INT,
  tool_results_compressed INT,
  assistant_responses_truncated INT,
  errors_fallen_through INT,
  session_uptime_hours DOUBLE
)
PARTITIONED BY (year STRING, month STRING)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
WITH SERDEPROPERTIES ('ignore.malformed.json' = 'true')
STORED AS TEXTFILE
LOCATION 's3://kiro-proxy-metrics-111452723372/metrics'
TBLPROPERTIES (
  'has_encrypted_data'='false',
  'projection.enabled'='true',
  'projection.year.type'='integer',
  'projection.year.range'='2024,2030',
  'projection.year.digits'='4',
  'projection.month.type'='integer',
  'projection.month.range'='1,12',
  'projection.month.digits'='2',
  'storage.location.template'='s3://kiro-proxy-metrics-111452723372/metrics/year=${year}/month=${month}'
);
```

Partition projection means Athena discovers partitions automatically from the S3 path — no need for `MSCK REPAIR TABLE` or manual `ALTER TABLE ADD PARTITION`.

## S3 Object Layout

```
s3://kiro-proxy-metrics-111452723372/
  metrics/
    year=2026/
      month=07/
        {install_id}/
          2026-07-20.json
          2026-07-21.json
        {install_id_2}/
          2026-07-20.json
      month=08/
        ...
```

## Sample Payload

```json
{
  "install_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "proxy_version": "0.4.0",
  "report_date": "2026-07-20",
  "requests_total": 147,
  "requests_compressed": 142,
  "bytes_saved": 2847392,
  "est_tokens_saved": 711848,
  "est_cost_saved_usd": 2.1355,
  "avg_savings_pct": 43.7,
  "images_stripped": 23,
  "tool_results_compressed": 89,
  "assistant_responses_truncated": 67,
  "errors_fallen_through": 5,
  "session_uptime_hours": 8.5
}
```

## Sample Queries

### Unique users this month
```sql
SELECT COUNT(DISTINCT install_id) AS active_users
FROM kiro_proxy.daily_metrics
WHERE year = '2026' AND month = '07';
```

### Total savings this month
```sql
SELECT
  SUM(requests_total) AS total_requests,
  SUM(requests_compressed) AS compressed,
  SUM(bytes_saved) / 1048576.0 AS mb_saved,
  SUM(est_cost_saved_usd) AS usd_saved,
  AVG(avg_savings_pct) AS avg_compression_pct
FROM kiro_proxy.daily_metrics
WHERE year = '2026' AND month = '07';
```

### Daily trend
```sql
SELECT report_date, COUNT(DISTINCT install_id) AS users,
       SUM(requests_total) AS requests, SUM(est_cost_saved_usd) AS savings
FROM kiro_proxy.daily_metrics
WHERE year = '2026' AND month = '07'
GROUP BY report_date
ORDER BY report_date;
```

## Failure Modes

All failures are silent (proxy never crashes due to telemetry):

| Failure | Behavior | User visibility |
|---------|----------|-----------------|
| boto3 not importable | Skip, log DEBUG | `kiro-proxy telemetry status` shows "last_error: boto3 not installed" |
| Credentials expired/missing | Skip, log DEBUG | Shows "last_error: NoCredentialsError: ..." |
| Network unreachable | Skip, log DEBUG | Shows "last_error: EndpointConnectionError: ..." |
| Permission denied (403) | Skip, log DEBUG | Shows "last_error: ClientError: AccessDenied" |
| Payload error (400) | Skip, log DEBUG | Shows "last_error: ClientError: ..." |

## Privacy

- `install_id` is a random UUID — not correlated to username or machine
- No conversation content is ever included
- Only aggregate counts and byte totals
- No PII collected
- Users can opt out completely

## AWS Resources

| Resource | ARN/Name |
|----------|----------|
| S3 metrics bucket | `kiro-proxy-metrics-111452723372` |
| S3 Athena results | `kiro-proxy-athena-results-111452723372` |
| Glue database | `kiro_proxy` |
| Glue table | `daily_metrics` |
| AWS account | `111452723372` (ai-platform-dev) |
| Region | `us-east-1` |
| Required IAM permissions | `s3:PutObject` on `kiro-proxy-metrics-111452723372/metrics/*` |
