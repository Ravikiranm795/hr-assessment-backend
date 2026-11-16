from app.core import branding_text
from app.core.config import Settings, settings
from app.main import create_app, discover_routers


def test_openapi_title_follows_app_name(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "Foo")
    app = create_app(Settings())
    assert app.openapi()["info"]["title"] == "Foo"


def test_app_slug_is_derived_from_name() -> None:
    assert Settings(app_name="Foo Bar & Baz!").app_slug == "foo-bar-baz"


def test_rename_keeps_jwt_issuer_and_refresh_cookie_name() -> None:
    renamed = Settings(app_name="Foo")
    assert renamed.jwt_issuer == Settings().jwt_issuer != renamed.app_slug
    assert renamed.refresh_cookie_name == Settings().refresh_cookie_name


def test_watermark_text_uses_vendor(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vendor_name", "Acme Widgets")
    assert branding_text.watermark() == "Powered by Acme Widgets"


def test_discover_routers_skips_modules_without_router(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "probe_modules"
    (pkg / "has_router").mkdir(parents=True)
    (pkg / "no_router").mkdir()
    for init in (pkg / "__init__.py", pkg / "has_router" / "__init__.py", pkg / "no_router" / "__init__.py"):
        init.write_text("")
    (pkg / "has_router" / "router.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/probe')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import probe_modules

    found = discover_routers(probe_modules)
    assert [r.prefix for r in found] == ["/probe"]
