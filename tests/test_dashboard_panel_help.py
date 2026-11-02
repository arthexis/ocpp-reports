"""Panel help is compact by default and accessible by pointer and keyboard."""
from fastapi.testclient import TestClient

from ocpp_reports import web


def test_five_panel_explanations_are_in_accessible_popovers(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "snapshot", lambda *args, **kwargs: {
        "sources": [], "connectors": [], "transactions": [], "events": [],
    })
    response = TestClient(web.create_app(tmp_path / "unused")).get("/")
    assert response.status_code == 200
    html = response.text
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
    assert ".panel-help:hover .panel-help-text" in html
    assert ".panel-help:focus-within .panel-help-text" in html
    assert ".panel-help.is-open .panel-help-text" in html
    assert 'trigger.addEventListener("click"' in html
    assert 'event.key === "Escape"' in html
    assert 'event.target.closest(".panel-help")' in html
    # Existing caveats have not disappeared; only their display is collapsed.
    for note in (
        "Una falla antigua también puede contarse como dato antiguo.",
        "No sumar C0 nuevamente en totales.",
        "SoC: primera y última medición reportada",
        "La recarga de la página no confirma datos nuevos del cargador.",
        "Eventos consecutivos de la misma acción",
    ):
        assert note in html
    assert 'Conectado*</th>' not in html
    assert 'Conectado</th>' in html
    assert 'role="region" aria-label="Tabla de conectores"' in html
