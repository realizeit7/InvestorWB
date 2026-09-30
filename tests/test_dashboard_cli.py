"""Dashboard views render; owner actions need the CSRF token; CLI paths run on fixture data."""

import io
from types import SimpleNamespace

import pytest

from equity_monitor.app import open_app
from equity_monitor.cli import main
from equity_monitor.dashboard import server
from equity_monitor.decisions.allocation import propose
from equity_monitor.decisions.recommend import review_portfolio
from equity_monitor.fixtures import AS_OF, build_demo
from equity_monitor.monitoring.alerts import create_alert
from equity_monitor.util import Clock


@pytest.fixture
def demo_home(tmp_path):
    home = tmp_path / "home"
    app = open_app(home, clock=Clock(AS_OF), settings_path="none", policy_path="none")
    d = build_demo(app)
    review_portfolio(app, d["portfolio_id"])
    propose(app, d["portfolio_id"])
    create_alert(app, "t1", "HEALTH", "Test alert", "body", severity="MATERIAL")
    app.conn.close()
    return home, d


def _call(wsgi, path, method="GET", body=b"", qs=""):
    out = {}

    def start(status, headers):
        out["status"] = status
    env = {"PATH_INFO": path, "QUERY_STRING": qs, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(body)),
           "wsgi.input": io.BytesIO(body)}
    html = b"".join(wsgi(env, start)).decode()
    return out["status"], html


def test_dashboard_views(demo_home):
    home, d = demo_home
    wsgi = server.make_wsgi(SimpleNamespace(home=str(home), policy="none", settings="none"))
    for path in ("/", "/allocation", "/health", "/inbox", f"/company/{d['securities']['ZZEXT']['security_id']}"):
        status, html = _call(wsgi, path)
        assert status.startswith("200"), (path, html[:500])
        assert "FIXTURE" in html and "PREVIEW" in html
    status, html = _call(wsgi, "/")
    assert "EXIT" in html and "ETF · tracked only" in html
    status, _ = _call(wsgi, "/alerts/ack", "POST", b"alert_id=x&csrf=wrong")
    assert status.startswith("403")


def test_cli_smoke(demo_home, capsys):
    home, _ = demo_home
    base = ["--home", str(home), "--policy", "none", "--settings", "none"]
    main(base + ["show", "--portfolio", "demo-fixture"])
    main(base + ["lots", "--portfolio", "demo-fixture"])
    main(base + ["valuation", "show", "ZZADD"])
    main(base + ["valuation", "reverse", "ZZADD", "--variable", "wacc"])
    main(base + ["thesis", "show", "ZZADD"])
    main(base + ["alerts", "list"])
    main(base + ["jobs", "status"])
    main(base + ["health"])
    main(base + ["policy", "show"])
    out = capsys.readouterr().out
    assert "FIXTURE" in out and "reverse DCF for wacc" in out and "never run" not in out.split("# System health")[0]
