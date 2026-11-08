import json
import logging
import os
import signal
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterable

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

log = logging.getLogger(__name__)

# Fields of GET /v2/customers/my/balance exported as the `kind` attribute.
KINDS = ("month_to_date_balance", "account_balance", "month_to_date_usage")


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

    def products(_: CallbackOptions) -> Iterable[Observation]:
        # "preview" is the daily-updated invoice for the current billing period.
        items = get("/customers/my/invoices/preview/summary")["product_charges"].get("items") or []
        return [Observation(float(i["amount"]), {"product": i["name"]}) for i in items]

    meter = provider.get_meter(__name__)
    meter.create_observable_gauge(
        "digitalocean.billing.balance",
        callbacks=[balance],
        unit="USD",
        description="DigitalOcean customer balance and month-to-date usage.",
    )
    meter.create_observable_gauge(
        "digitalocean.billing.product.charges",
        callbacks=[products],
        unit="USD",
        description="Current billing period usage charges per product (invoice preview).",
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
