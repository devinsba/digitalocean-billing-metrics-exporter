from digitalocean_billing_metrics_exporter import main


def test_main_runs(capsys):
    main()
    assert capsys.readouterr().out
