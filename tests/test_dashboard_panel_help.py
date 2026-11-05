"""Panel help is compact by default and accessible by pointer and keyboard."""

def test_five_panel_explanations_are_in_accessible_popovers(empty_dashboard_html):
    html = empty_dashboard_html
    for identifier, label in (
        ("operations", "Estado de los cargadores"),
        ("connectors", "Conectores"),
        ("transactions", "Transacciones recientes"),
        ("synchronization", "Sincronización"),
        ("events", "Eventos recientes"),
    ):
        assert f'id="help-{identifier}" role="tooltip"' in html
        assert f'aria-controls="help-{identifier}" aria-expanded="false"' in html
        assert f'aria-label="Información sobre {label}"' in html
    assert html.count('class="panel-help-trigger"') == 5
    assert ".panel-help-text{display:none" in html
    assert ".panel-help:hover + .panel-help-text" in html
    assert ".panel-help:focus-within + .panel-help-text" in html
    assert ".panel-help.is-open + .panel-help-text" in html
    assert 'trigger.addEventListener("click"' in html
    assert 'event.key === "Escape"' in html
    assert 'event.target.closest(".panel-help")' in html
    # Existing caveats have not disappeared; only their display is collapsed.
    for note in (
        "Una falla con origen atrasado también puede contarse como dato incierto.",
        "No sumar C0 nuevamente en totales.",
        "SoC: primera y última medición reportada",
        "La recarga de la página no confirma datos nuevos del cargador.",
        "Eventos consecutivos de la misma acción",
    ):
        assert note in html
    assert 'Conectado*</th>' not in html
    assert 'Conectado</th>' in html
    assert 'role="region" aria-label="Tabla de conectores"' in html


def test_mobile_help_reveals_in_flow_without_touch_focus_or_overlay(monkeypatch, tmp_path):
    """Touch click cannot be intercepted by a popup rendered over its trigger."""
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    page = TestClient(web.create_app(tmp_path / "unused")).get("/")
    assert page.status_code == 200
    html = page.text
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".panel-help.is-open + .panel-help-text" in css
    assert "@media(hover:none){.panel-help:not(.is-open) + .panel-help-text{display:none}}" in css
    assert "position:static;flex:0 0 100%" in css
    assert "max-height:min(50vh,320px);overflow:auto" in css
    assert 'trigger.addEventListener("click"' in html
    assert 'event.key === "Escape"' in html
    # Each help is a sibling inside the heading: its content expands before
    # the connector table, rather than intercepting or covering status badges.
    assert '</button></span><span class="panel-help-text" id="help-connectors"' in html
    assert '.panel-help-text{position:fixed' not in css
