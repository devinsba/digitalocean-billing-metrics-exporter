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
SUMMARY = {
    "invoice_uuid": "22737513-0ea7-4206-8ceb-98a575af7681",
    "product_charges": {
        "name": "Product usage charges",
        "amount": "12.34",
        "items": [
            {"amount": "10.00", "name": "Spaces Subscription", "count": "1"},
            {"amount": "2.34", "name": "Database Clusters", "count": "1"},
        ],
    },
}
RESPONSES = {
    "/customers/my/balance": BALANCE,
    "/customers/my/invoices/preview/summary": SUMMARY,
}


def collect(get) -> dict:
    """{metric name: {attribute value: point value}}"""
    reader = InMemoryMetricReader()
    register(MeterProvider(metric_readers=[reader]), get)
    data = reader.get_metrics_data()
    if data is None:
        return {}
    out = {}
    for m in data.resource_metrics[0].scope_metrics[0].metrics:
        assert m.unit == "USD"
        out[m.name] = {next(iter(p.attributes.values())): p.value for p in m.data.data_points}
    return out


def test_balance_metrics():
    assert collect(RESPONSES.__getitem__) == {
        "digitalocean.billing.balance": {
            "month_to_date_balance": 23.44,
            "account_balance": 12.23,
            "month_to_date_usage": 11.21,
        },
        "digitalocean.billing.product.charges": {
            "Spaces Subscription": 10.0,
            "Database Clusters": 2.34,
        },
    }


def test_api_error_drops_series():
    def boom(path):
        raise OSError("api down")

    assert collect(boom) == {}


def test_one_endpoint_failing_keeps_the_other():
    def get(path):
        if "invoices" in path:
            raise OSError("api down")
        return BALANCE

    assert list(collect(get)) == ["digitalocean.billing.balance"]


def test_main_requires_token(monkeypatch):
    monkeypatch.delenv("DO_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main()
