"""Unit tests for the sanitizer, the contrast helper, the upload checks and the provider catalog."""

import json

import pytest

from app.core.errors import AppError
from app.modules.m04_org_settings import catalog
from app.modules.m04_org_settings.contrast import check_contrast, check_text_contrast, normalize_hex
from app.modules.m04_org_settings.sanitize import sanitize_html
from app.modules.m04_org_settings.uploads import inspect_image, inspect_pdf
from tests.m04_org_settings.conftest import ico_bytes, jpeg_bytes, pdf_bytes, png_bytes

# --------------------------------------------------------------------------- sanitizer


@pytest.mark.parametrize(
    "dirty",
    [
        "<script>alert(1)</script><p>hi</p>",
        '<p onclick="x()">hi</p>',
        '<img src=x onerror="alert(1)">',
        '<a href="javascript:alert(1)">x</a>',
        '<a href="JaVaScRiPt:alert(1)">x</a>',
        '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
        '<a href="vbscript:x">x</a>',
        '<iframe src="https://evil.example"></iframe>',
        '<p style="background:url(javascript:alert(1))">x</p>',
        "<svg onload=alert(1)><circle/></svg>",
        "<object data=x></object><embed src=x>",
        "<style>body{display:none}</style><p>x</p>",
        '<form action="https://evil.example"><input></form>',
        "<!-- comment --><p>x</p>",
    ],
)
def test_sanitize_removes_active_content(dirty):
    clean = sanitize_html(dirty).lower()
    for needle in ("<script", "onclick", "onerror", "onload", "javascript:", "vbscript:", "data:", "<iframe"):
        assert needle not in clean
    for needle in ("<style", "<img", "<svg", "<object", "<embed", "<form", "<input", "style=", "<!--"):
        assert needle not in clean


def test_sanitize_keeps_formatting_and_safe_links():
    html = (
        '<h1>Title</h1><p>Some <strong>bold</strong> and <em>italic</em> <a href="https://example.com/p" '
        'title="t">link</a> <a href="mailto:a@b.co">mail</a></p><ul><li>one</li></ul>'
        '<table><tr><td colspan="2">cell</td></tr></table>'
    )
    clean = sanitize_html(html)
    for fragment in ("<h1>Title</h1>", "<strong>bold</strong>", "<em>italic</em>", "<li>one</li>"):
        assert fragment in clean
    assert 'href="https://example.com/p"' in clean and 'href="mailto:a@b.co"' in clean
    assert 'rel="noopener noreferrer"' in clean
    assert 'colspan="2"' in clean


def test_sanitize_script_content_dropped_not_escaped():
    assert "alert" not in sanitize_html("<script>alert(1)</script>ok")
    assert sanitize_html("") == ""


def test_sanitize_denies_relative_and_protocol_relative_urls():
    clean = sanitize_html('<a href="//evil.example/x">a</a><a href="/local">b</a>')
    assert "evil.example" not in clean and 'href="/local"' not in clean


# --------------------------------------------------------------------------- contrast


def test_check_contrast_known_values():
    assert check_contrast("#000000", "#FFFFFF") == pytest.approx(21.0, rel=1e-3)
    assert check_contrast("#fff", "#fff") == pytest.approx(1.0)
    assert check_contrast("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.02)


def test_normalize_hex():
    assert normalize_hex("#abc") == "#AABBCC"
    assert normalize_hex("#1b2a4a") == "#1B2A4A"
    for bad in ("1B2A4A", "#12345", "#GGGGGG", "red", ""):
        with pytest.raises(ValueError):
            normalize_hex(bad)


def test_text_contrast_picks_best_text_colour():
    navy = check_text_contrast("#1B2A4A")
    assert navy.passes and navy.text_color == "#FFFFFF"
    coral = check_text_contrast("#F26B5B")
    assert coral.passes and coral.text_color != "#FFFFFF"
    grey = check_text_contrast("#7A7A7A")
    assert not grey.passes and grey.ratio < 4.5


# --------------------------------------------------------------------------- uploads


def test_inspect_image_detects_type_and_size():
    assert inspect_image(png_bytes(32, 20)).content_type == "image/png"
    assert (inspect_image(png_bytes(32, 20)).width, inspect_image(png_bytes(32, 20)).height) == (32, 20)
    assert inspect_image(jpeg_bytes(40, 30)).content_type == "image/jpeg"
    assert inspect_image(ico_bytes(), allow_ico=True).content_type == "image/x-icon"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello world, not an image",
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>",
        b"GIF89a" + b"\x00" * 20,
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 10,  # truncated header
        png_bytes(10, 10),  # below minimum size
        ico_bytes(),  # ico not allowed for logos
    ],
)
def test_inspect_image_rejects(data):
    with pytest.raises(AppError) as exc:
        inspect_image(data)
    assert exc.value.code == "UPLOAD_INVALID"


def test_inspect_pdf():
    assert inspect_pdf(pdf_bytes()) == "application/pdf"
    for bad in (b"not a pdf", b"", pdf_bytes(b"/JavaScript (app.alert(1))"), pdf_bytes(b"/Launch")):
        with pytest.raises(AppError) as exc:
            inspect_pdf(bad)
        assert exc.value.code == "UPLOAD_INVALID"


# --------------------------------------------------------------------------- provider catalog


def test_catalog_fallback_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "MODELS_FILE", tmp_path / "missing.json")
    data = catalog.provider_catalog()
    ids = [p["id"] for p in data]
    assert ids == ["claude", "gemini", "openai", "openai-compatible", "cli"]
    assert next(p for p in data if p["id"] == "claude")["default_model"] == "claude-haiku-5-5"
    assert next(p for p in data if p["id"] == "openai-compatible")["requires_base_url"] is True
    assert next(p for p in data if p["id"] == "cli")["modes"] == ["cli"]


@pytest.mark.parametrize("content", ["not json", "[]", '{"claude": 5}', '{"providers": null}', "null"])
def test_catalog_survives_garbage_file(tmp_path, monkeypatch, content):
    path = tmp_path / "ai_models.json"
    path.write_text(content)
    monkeypatch.setattr(catalog, "MODELS_FILE", path)
    assert next(p for p in catalog.provider_catalog() if p["id"] == "claude")["default_model"] == "claude-haiku-5-5"


def test_catalog_reads_defaults_from_file(tmp_path, monkeypatch):
    path = tmp_path / "ai_models.json"
    path.write_text(json.dumps({"claude": {"default_model": "claude-x", "models": ["claude-x", "claude-y"]}}))
    monkeypatch.setattr(catalog, "MODELS_FILE", path)
    claude = next(p for p in catalog.provider_catalog() if p["id"] == "claude")
    assert claude["default_model"] == "claude-x" and claude["models"] == ["claude-x", "claude-y"]
    # untouched providers keep fallback values
    assert next(p for p in catalog.provider_catalog() if p["id"] == "gemini")["default_model"]
