import logging
import os
import signal
import sys
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import requests
import structlog
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

log = structlog.get_logger(__name__)


def configure_logging(level: str) -> None:
    """JSON logs to stderr for structlog and stdlib (OTel SDK) records alike."""
    shared = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


# Fields of GET /v2/customers/my/balance exported as the `kind` attribute.
KINDS = ("month_to_date_balance", "account_balance", "month_to_date_usage")
# Fields of a billing insights data point that identify a resource.
RESOURCE_ATTRS = ("sku", "region", "description", "group_description")


def fetch(token: str, api_url: str, path: str) -> dict:
    resp = requests.get(
        f"{api_url.rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "digitalocean-billing-metrics-exporter",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def register(provider: MeterProvider, get: Callable[[str], dict]) -> None:
    # Callbacks run on every export interval; on API error the SDK logs it and the series go absent.
    def balance(_: CallbackOptions) -> Iterable[Observation]:
        body = get("/customers/my/balance")
        log.info("balance", month_to_date_usage=body["month_to_date_usage"])
        return [Observation(float(body[k]), {"kind": k}) for k in KINDS]

    def resources(_: CallbackOptions) -> Iterable[Observation]:
        # Billing insights are per resource per day, from nightly invoice items, so they're
        # available mid-month (unlike invoices). Sum the month so far per resource.
        team = get("/account")["account"]["team"]["uuid"]
        today = datetime.now(UTC).date()
        base = f"/billing/do:team:{team}/insights/{today.replace(day=1)}/{today}?per_page=200"
        totals: dict[tuple, float] = defaultdict(float)
        page, pages = 1, 1
        while page <= pages:
            body = get(f"{base}&page={page}")
            pages = int(body.get("total_pages") or 1)
            for p in body.get("data_points") or []:
                totals[tuple(p.get(k) or "" for k in RESOURCE_ATTRS)] += float(p["total_amount"])
            page += 1
        log.info("resource_charges", resources=len(totals), month_to_date=sum(totals.values()))
        return [
            Observation(v, dict(zip(RESOURCE_ATTRS, k, strict=True))) for k, v in totals.items()
        ]

    meter = provider.get_meter(__name__)
    meter.create_observable_gauge(
        "digitalocean.billing.balance",
        callbacks=[balance],
        unit="USD",
        description="DigitalOcean customer balance and month-to-date usage.",
    )
    meter.create_observable_gauge(
        "digitalocean.billing.resource.charges",
        callbacks=[resources],
        unit="USD",
        description="Month-to-date estimated charges per resource (billing insights).",
    )


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
    token = os.environ.get("DO_TOKEN")
    if not token:
        sys.exit("DO_TOKEN environment variable is required")
    api_url = os.environ.get("DO_API_URL") or "https://api.digitalocean.com/v2"

    os.environ.setdefault("OTEL_SERVICE_NAME", "digitalocean-billing-metrics-exporter")
    # Billing data changes slowly; poll every 5 min unless OTEL_METRIC_EXPORT_INTERVAL is set.
    os.environ.setdefault("OTEL_METRIC_EXPORT_INTERVAL", "300000")
    # Exporter and Resource read the standard OTEL_* env vars (endpoint, headers, ...).
    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    provider = MeterProvider(resource=Resource.create(), metric_readers=[reader])
    # Stable semconv: http.client.request.duration (s). Provider is not global, so pass it.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    RequestsInstrumentor().instrument(meter_provider=provider)
    SystemMetricsInstrumentor().instrument(meter_provider=provider)
    register(provider, lambda path: fetch(token, api_url, path))
    log.info("pushing_metrics_via_otlp")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    provider.shutdown()  # final export
