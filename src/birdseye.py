"""Fleet-wide metrics view for kiro-proxy (birdseye).

Queries the Athena table for aggregate stats across all installations.
Used by both the CLI (`kiro-proxy birdseye`) and the menu bar applet
("Fleet Metrics" button).

Requires: boto3 with valid SSO session for ai-platform-dev profile.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("kiro_proxy.birdseye")

_DATABASE = "kiro_proxy"
_WORKGROUP = "primary"
_OUTPUT_LOCATION = "s3://kiro-proxy-athena-results-111452723372/"
_PROFILE = "ai-platform-dev"
_REGION = "us-east-1"

_FLEET_QUERY = """\
SELECT
    COUNT(DISTINCT install_id) AS active_users,
    SUM(requests_total) AS total_requests,
    SUM(requests_compressed) AS total_compressed,
    SUM(bytes_saved) AS total_bytes_saved,
    SUM(est_tokens_saved) AS total_tokens_saved,
    ROUND(SUM(est_cost_saved_usd), 2) AS total_cost_saved_usd,
    ROUND(AVG(avg_savings_pct), 1) AS fleet_avg_savings_pct,
    SUM(images_stripped) AS total_images_stripped,
    SUM(errors_fallen_through) AS total_errors
FROM daily_metrics
WHERE year = '{year}' AND month = '{month}'
"""


def query_fleet_metrics(year: str | None = None, month: str | None = None) -> dict[str, Any] | None:
    """Run the fleet metrics query against Athena.

    Returns a dict of aggregate stats, or None on failure.
    """
    try:
        import boto3
    except ImportError:
        logger.error("boto3 not installed — cannot query fleet metrics")
        return None

    now = datetime.now(timezone.utc)
    if year is None:
        year = now.strftime("%Y")
    if month is None:
        month = now.strftime("%m")

    query = _FLEET_QUERY.format(year=year, month=month)

    try:
        session = boto3.Session(profile_name=_PROFILE, region_name=_REGION)
        athena = session.client("athena")

        # Start query
        response = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext={"Database": _DATABASE},
            ResultConfiguration={"OutputLocation": _OUTPUT_LOCATION},
        )
        execution_id = response["QueryExecutionId"]

        # Poll for completion (max 30s)
        for _ in range(30):
            status = athena.get_query_execution(QueryExecutionId=execution_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(1)

        if state != "SUCCEEDED":
            reason = status["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
            logger.error("Athena query %s: %s", state, reason)
            return None

        # Fetch results
        results = athena.get_query_results(QueryExecutionId=execution_id)
        rows = results["ResultSet"]["Rows"]
        if len(rows) < 2:
            return {"active_users": 0, "period": f"{year}-{month}"}

        columns = [col["VarCharValue"] for col in rows[0]["Data"]]
        values = [col.get("VarCharValue", "0") for col in rows[1]["Data"]]

        metrics = {}
        for col, val in zip(columns, values):
            try:
                if "." in val:
                    metrics[col] = float(val)
                else:
                    metrics[col] = int(val)
            except (ValueError, TypeError):
                metrics[col] = val

        metrics["period"] = f"{year}-{month}"
        return metrics

    except Exception as exc:
        logger.error("Fleet metrics query failed: %s", exc)
        return None


def format_metrics(metrics: dict[str, Any]) -> str:
    """Format fleet metrics into a human-readable string."""
    if not metrics or metrics.get("active_users", 0) == 0:
        period = metrics.get("period", "unknown") if metrics else "unknown"
        return f"  No data for {period}. Either no one has reported yet,\n  or AWS credentials have expired."

    period = metrics.get("period", "")
    users = metrics.get("active_users", 0)
    requests = metrics.get("total_requests", 0)
    compressed = metrics.get("total_compressed", 0)
    bytes_saved = metrics.get("total_bytes_saved", 0)
    tokens = metrics.get("total_tokens_saved", 0)
    cost = metrics.get("total_cost_saved_usd", 0.0)
    avg_pct = metrics.get("fleet_avg_savings_pct", 0.0)
    images = metrics.get("total_images_stripped", 0)
    errors = metrics.get("total_errors", 0)

    mb_saved = bytes_saved / (1024 * 1024)
    compress_rate = (compressed / requests * 100) if requests > 0 else 0

    lines = [
        f"  Period:            {period}",
        f"  Active users:      {users}",
        f"  Requests:          {requests:,} ({compress_rate:.0f}% compressed)",
        f"  Data saved:        {mb_saved:.1f} MB",
        f"  Tokens saved:      {tokens:,}",
        f"  Est cost saved:    ${cost:.2f}",
        f"  Avg compression:   {avg_pct:.1f}%",
        f"  Images stripped:   {images:,}",
    ]
    if errors > 0:
        error_rate = errors / requests * 100 if requests > 0 else 0
        lines.append(f"  Errors:            {errors:,} ({error_rate:.1f}% of requests)")

    return "\n".join(lines)


def main() -> None:
    """CLI entry point for kiro-proxy birdseye."""
    import argparse

    parser = argparse.ArgumentParser(description="Fleet-wide kiro-proxy metrics")
    parser.add_argument("--year", help="Year to query (default: current)")
    parser.add_argument("--month", help="Month to query (default: current)")
    args = parser.parse_args()

    print("\n\033[1mkiro-proxy birdseye\033[0m — fleet metrics\n")
    print("  Querying Athena...")

    metrics = query_fleet_metrics(year=args.year, month=args.month)
    if metrics is None:
        print("\n  \033[31m✗\033[0m Query failed. Check AWS credentials:")
        print("    aws sso login --profile ai-platform-dev")
        return

    print()
    print(format_metrics(metrics))
    print()


if __name__ == "__main__":
    main()
