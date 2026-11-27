"""Dashboard help uses shared tooltip and dialog outside the table layout."""
def test_dashboard_panel_explanations_share_dialog(empty_dashboard_html):
    html = empty_dashboard_html
    for label in ("Estado de los cargadores", "Conectores",
                  "Sesiones de carga", "Tarjetas RFID"):
        assert f'aria-label="Información sobre {label}"' in html
        assert f'data-help-title="{label}"' in html
    assert html.count('class="panel-help-trigger"') == 4
    assert html.count('aria-haspopup="dialog" aria-controls="panel-help-dialog"') == 4
    assert html.count('id="panel-help-dialog"') == 1
    assert '</main>\n\n<div id="panel-help-tooltip"' in html
    assert 'role="tooltip" hidden' in html
    assert '.panel-help-tooltip{position:fixed' in html
    assert '(hover: hover) and (pointer: fine)' in html
    assert 'showTooltip(trigger)' in html
    assert 'aria-labelledby="panel-help-title" aria-describedby="panel-help-description"' in html
    assert '.panel-help-dialog{position:fixed' in html
    assert 'help.showModal()' in html
    assert 'helpTrigger?.focus({preventScroll: true})' in html
    assert 'panel-help-text' not in html
    assert 'energía final positiva' in html
    assert 'energía final cero' in html
    assert 'Desconocido si falta evidencia' in html
    assert 'medidor acumulado' in html
    assert 'Último acceso: inicio de la sesión más reciente' in html
