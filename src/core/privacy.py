"""Privacy-safe values for operational telemetry tables."""

# Operational analytics only need counts, latency, and token totals. Keep
# user content in its user-owned product records (such as conversations), not
# duplicate it in long-lived telemetry rows.
REDACTED_OPERATIONAL_CONTENT = "[redacted]"
