"""Shared Gelectriic favicon contract; assets must be binary, packaged and tenant-neutral."""
from importlib import resources
from pathlib import Path
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient

from ocpp_reports.dashboard_login import route_permission
from ocpp_reports.web import create_app

SIZES = (16, 32, 180, 192, 512)
PATHS = ("/favicon.ico", *(f"/assets/favicon-{n}.png" for n in SIZES))
TEMPLATES = ("dashboard.html.j2", "device_detail.html.j2",
             "transaction_detail.html.j2")


@pytest.mark.parametrize("path", PATHS)
def test_favicon_routes_and_permissions(tmp_path, path):
    assert route_permission(path, "GET") is not None
    assert route_permission(path, "POST") is None
    app = create_app(tmp_path / "missing.sqlite", title="Porsche")
    response = TestClient(app).get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == (
        "image/x-icon" if path.endswith(".ico") else "image/png"
    )
    asset = resources.files("ocpp_reports").joinpath(
        "assets", path.rsplit("/", 1)[-1]).read_bytes()
    assert response.content == asset
    assert len(asset) > 100
    if path.endswith(".ico"):
        assert response.content[:4] == b"\\x00\\x00\\x01\\x00"
        count = struct.unpack_from("<H", response.content, 4)[0]
        sizes = {tuple((256 if n == 0 else n) for n in response.content[6 + i*16:8 + i*16])
                 for i in range(count)}
        assert (16, 16) in sizes and (32, 32) in sizes
    else:
        assert response.content[:8] == bytes.fromhex("89504e470d0a1a0a")
        size = int(path.split("favicon-")[1].split(".")[0])
        assert struct.unpack_from(">II", response.content, 16) == (size, size)
        assert response.content[25] == 6  # PNG true-color RGBA

def test_templates_share_icons_and_do_not_replace_branding():
    root = resources.files("ocpp_reports").joinpath("templates")
    links = root.joinpath("favicon_links.html.j2").read_text()
    assert 'rel="apple-touch-icon"' in links
    assert 'rel="icon"' in links
    for path in PATHS:
        assert f'href="{path}"' in links
    for name in TEMPLATES:
        html = root.joinpath(name).read_text()
        assert '{% include "favicon_links.html.j2" %}' in html
        assert "brand_image_url" in html
        assert "<title>" in html


def test_no_favicon_route_wildcard(tmp_path):
    app = create_app(tmp_path / "missing.sqlite")
    client = TestClient(app)
    assert client.get("/assets/favicon-secret.png").status_code == 404
    assert route_permission("/assets/favicon-secret.png", "GET") is None


def test_wheel_contains_all_favicon_assets():
    wheels = list(Path("dist").glob("ocpp_reports-*.whl"))
    if not wheels:
        pytest.skip("Wheel verification runs after building distribution")
    with zipfile.ZipFile(wheels[-1]) as whl:
        names = whl.namelist()
    for path in PATHS:
        assert any(name.endswith("assets/" + path.rsplit("/", 1)[-1])
                   for name in names)
