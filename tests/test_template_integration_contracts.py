"""Cross-check Jinja templates and the API routes consumed by primary pages.

These are structural integration checks: they catch template parse errors and
frontend/backend route drift without requiring live broker credentials or
market hours.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "app" / "templates"


def test_every_html_template_compiles():
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    templates = sorted(TEMPLATE_DIR.glob("*.html"))

    assert templates, "No HTML templates found"

    for path in templates:
        # get_template parses the complete file, including inherited blocks,
        # inline styles, and embedded JavaScript text.
        env.get_template(path.name)


def test_primary_page_api_contracts_are_registered():
    registered_paths = {
        route.path
        for route in app.routes
        if isinstance(getattr(route, "path", None), str)
    }

    # Dashboard + Analysis + MCX workspace integrations.
    required_paths = {
        "/api/dashboard/status",
        "/api/market/symbols",
        "/api/market/mcx/catalog",
        "/api/market/mcx/contracts",
        "/api/market/mcx/quote/{commodity}",
        "/api/market/mcx/analysis/{commodity}",
        "/api/angel/live-feed",
        "/api/portfolio/positions",
        "/api/paper-trade/open",
        "/api/analysis/ai/{symbol}",
        "/api/strategy/recommend/{symbol}",
    }

    missing = sorted(required_paths - registered_paths)
    assert not missing, (
        "A primary HTML template calls API routes that are not registered: "
        + ", ".join(missing)
    )
