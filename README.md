# digitalocean-billing-metrics-exporter

Prometheus exporter for DigitalOcean billing metrics.

```sh
uv sync
uv run pytest
DO_TOKEN=... uv run digitalocean-billing-metrics-exporter
```

Env: `DO_TOKEN` (required, read access to billing), `DO_API_URL`, `LOG_LEVEL`, plus the standard
`OTEL_*` vars (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, ...).

Every `OTEL_METRIC_EXPORT_INTERVAL` ms (default 300000) it pushes over OTLP/HTTP:

- `digitalocean.billing.balance` (USD), attribute `kind` = `month_to_date_balance` |
  `account_balance` | `month_to_date_usage`, from `GET /v2/customers/my/balance`.
- `digitalocean.billing.resource.charges` (USD), attributes `sku`, `region`, `description`,
  `group_description`: month-to-date estimated charges per resource, summed from
  `GET /v2/billing/do:team:<uuid>/insights/<1st of month>/<today>` (nightly invoice items, so
  available mid-month; a resource shows up the day after its first usage). The team is the one
  that owns `DO_TOKEN`.

If an API call fails, that metric has no points for the interval (alert on absence).

Releases: push a `v*` tag to build and publish `ghcr.io/devinsba/digitalocean-billing-metrics-exporter`.
