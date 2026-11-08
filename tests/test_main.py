from datetime import UTC, datetime

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from digitalocean_billing_metrics_exporter import main, register

BALANCE = {
    "month_to_date_balance": "23.44",
    "account_balance": "12.23",
    "month_to_date_usage": "11.21",
    "generated_at": "2019-07-09T15:01:12Z",
}
TEAM = "12345678-1234-1234-1234-123456789012"
DROPLET = {"sku": "1-DO-DROP-0109", "region": "nyc3", "description": "web (c-2-4GiB)"}
K8S = {
    "sku": "1-KS-K8SWN-00109",
    "region": "nyc3",
    "description": "3 nodes",
    "group_description": "prod",
}
INSIGHT_PAGES = {
    1: {
        "data_points": [
            {
                **DROPLET,
                "group_description": "",
                "start_date": "2026-11-01",
                "total_amount": "0.86",
            },
            {
                **DROPLET,
                "group_description": "",
                "start_date": "2026-11-02",
                "total_amount": "0.86",
            },
        ],
        "total_pages": 2,
        "current_page": "1",
    },
    2: {
        "data_points": [{**K8S, "start_date": "2026-11-01", "total_amount": "2.57"}],
        "total_pages": 2,
        "current_page": "2",
    },
}


def get(path):
    if path == "/customers/my/balance":
        return BALANCE
    if path == "/account":
        return {"account": {"team": {"uuid": TEAM, "name": "Personal"}}}
    today = datetime.now(UTC).date()
    prefix = f"/billing/do:team:{TEAM}/insights/{today.replace(day=1)}/{today}?per_page=200&page="
    assert path.startswith(prefix), path
    return INSIGHT_PAGES[int(path.removeprefix(prefix))]


def collect(get) -> dict:
    """{metric name: [(attributes, value), ...]}"""
    reader = InMemoryMetricReader()
    register(MeterProvider(metric_readers=[reader]), get)
    data = reader.get_metrics_data()
    if data is None:
        return {}
    out = {}
    for m in data.resource_metrics[0].scope_metrics[0].metrics:
        assert m.unit == "USD"
        out[m.name] = sorted(((dict(p.attributes), p.value) for p in m.data.data_points), key=str)
    return out


def test_metrics():
    out = collect(get)
    assert out["digitalocean.billing.balance"] == sorted(
        [
            ({"kind": "month_to_date_balance"}, 23.44),
            ({"kind": "account_balance"}, 12.23),
            ({"kind": "month_to_date_usage"}, 11.21),
        ],
        key=str,
    )
    # Daily rows for the same resource are summed; all pages are read.
    assert out["digitalocean.billing.resource.charges"] == sorted(
        [({**DROPLET, "group_description": ""}, 1.72), (K8S, 2.57)], key=str
    )


def test_no_usage_yet():
    def empty(path):
        return {"current_page": "1"} if path.startswith("/billing/") else get(path)

    assert "digitalocean.billing.resource.charges" not in collect(empty)


def test_api_error_drops_series():
    def boom(path):
        raise OSError("api down")

    assert collect(boom) == {}


def test_one_endpoint_failing_keeps_the_other():
    def flaky(path):
        if path.startswith("/billing/"):
            raise OSError("api down")
        return get(path)

    assert list(collect(flaky)) == ["digitalocean.billing.balance"]


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv("DO_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main()
