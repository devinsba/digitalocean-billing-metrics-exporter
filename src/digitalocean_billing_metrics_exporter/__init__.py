import json
import logging
import os
import signal
import sys
import threading
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

log = logging.getLogger(__name__)

# Fields of GET /v2/customers/my/balance exported as the `kind` attribute.
KINDS = ("month_to_date_balance", "account_balance", "month_to_date_usage")
# Fields of a billing insights data point that identify a resource.
RESOURCE_ATTRS = ("sku", "region", "description", "group_description")


def fetch(token: str, api_url: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "digitalocean-billing-metrics-exporter",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def register(provider: MeterProvider, get: Callable[[str], dict]) -> None:
    # Callbacks run on every export interval; on API error the SDK logs it and the series go absent.
    def balance(_: CallbackOptions) -> Iterable[Observation]:
        body = get("/customers/my/balance")
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
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
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
    register(provider, lambda path: fetch(token, api_url, path))
    log.info("Pushing metrics via OTLP")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    provider.shutdown()  # final export
