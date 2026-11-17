"""Regression checks for compact mobile navigation without losing search access."""

def test_mobile_header_preserves_search_and_timezone_controls(empty_dashboard_html):
    html = empty_dashboard_html
    css = html.split("<style>", 1)[1].split("</style>", 1)[0]
    compact = css.split("/* Narrow phones:", 1)[1].split("\n}", 1)[0]
    assert "@media(max-width:520px)" in compact
    assert "header{padding:.5rem .9rem;gap:.3rem}" in compact
    assert ".header-filters label{gap:0;font-size:0;line-height:0}" in compact
    assert "min-height:40px" in compact
    assert 'id="dashboard-search"' in html
    assert 'name="filter"' in html
    assert 'placeholder="Origen, cargador, ubicación o estado"' in html
    assert '<label for="dashboard-search">Filtrar <input' in html
    assert 'id="timezone-choice"' in html
    assert 'id="dashboard-filters"' in html


def test_mobile_filter_profile_share_row_and_connector_cards_use_two_columns(dashboard_source):
    source = dashboard_source
    css = source.split("<style>", 1)[1].split("</style>", 1)[0]
    mobile = css.split("/* Mobile header and connector cards:", 1)[1].split("\n}", 1)[0]
    assert "@media(max-width:520px)" in mobile
    assert "header .brand{flex:0 0 100%;width:100%}" in mobile
    assert ".header-filters{flex:1 1 0;width:auto;min-width:0;flex-wrap:nowrap}" in mobile
    assert ".profile-menu summary{min-width:44px;min-height:44px}" in mobile
    assert ".connectors-panel tbody tr{display:grid;grid-template-columns:minmax(0,.76fr) minmax(0,1.24fr)" in mobile
    assert ".connectors-panel td{display:flex;flex-direction:column;" in mobile
    assert ".connectors-panel td.empty{grid-column:1/-1;grid-row:auto}" in mobile
    assert '<th>Recibido</th>' in source
    assert 'data-label="Recibido"' in source
    assert '<th>Estado informado</th>' not in source
    assert 'data-label="Estado informado"' not in source
    assert "c.status_observed_at|display_date(timezone)" in source
    assert "no la hora de recepción del snapshot" in source


def test_mobile_power_qualifier_stays_inline_and_values_outweigh_labels(dashboard_source):
    source = dashboard_source
    css = source.split("<style>", 1)[1].split("</style>", 1)[0]
    mobile = css.split("/* Mobile header and connector cards:", 1)[1].split("\n}", 1)[0]
    assert "overflow-wrap:anywhere;font-size:1.18rem" in mobile
    assert "padding:.1rem .02rem calc(.1rem + 1px)" in mobile
    assert 'td[data-label="Cargador"] .charger-session-link,' in mobile
    assert 'td[data-label="Conector"] .charger-session-link{' in mobile
    assert "display:inline-block;position:relative;min-height:0" in mobile
    assert "bottom:0;height:44px" in mobile
    assert ".connectors-panel td::before{flex:0 0 auto;width:100%;font-size:.77rem" in mobile
    assert "column-gap:.28rem;row-gap:.14rem" in mobile
    assert '.connectors-panel td[data-label="Potencia"] .connector-power{display:inline-flex;' in mobile
    assert "white-space:normal" in mobile
    assert '.connectors-panel td[data-label="Potencia"] .connector-power .note{font-size:.73rem}' in mobile
    cell = source.split('<td data-label="Potencia" class="numeric">', 1)[1].split("</td>", 1)[0]
    assert cell.startswith('<span class="connector-power">')
    assert cell.endswith("</span>")
    assert ">nominal</span>" in cell
    assert ">medida</span>" in cell
    assert "c.power_fresh" in cell and "c.rated_power_w" in cell


def test_tablet_header_and_tables_preserve_mobile_and_desktop_semantics(dashboard_source):
    source = dashboard_source
    css = source.split("<style>", 1)[1].split("</style>", 1)[0]
    tablet = css.split("/* Tablet and compact desktop:", 1)[1]
    assert "@media(min-width:521px) and (max-width:1100px)" in tablet
    assert "header .brand{flex:0 0 100%;width:100%}" in tablet
    assert "header .header-filters{flex:1 1 0;width:auto;min-width:0;" in tablet
    assert "header .header-filters .unified-filter{width:100%;min-width:0}" in tablet
    assert "header .profile-menu summary{min-width:44px;min-height:44px}" in tablet
    assert ".connectors-panel table{min-width:0;width:100%;table-layout:fixed}" in tablet
    for column in (2, 6, 7, 8):
        assert f".connectors-panel th:nth-child({column})" in tablet
        assert f".connectors-panel td:nth-child({column})" in tablet
    for column in (2, 5, 8, 9):
        assert f".transactions-panel th:nth-child({column})" in tablet
        assert f".transactions-panel tbody td:nth-child({column})" in tablet
    wide_tablet = css.split("/* Power is useful at tablet widths", 1)[1]
    assert "@media(min-width:760px) and (max-width:1100px)" in wide_tablet
    assert ".connectors-panel th:nth-child(6),.connectors-panel td:nth-child(6){display:table-cell}" in wide_tablet
    assert ".connectors-panel th:nth-child(6){width:21%}" in wide_tablet
    assert "<th>Estado</th>" in source
    assert "<th>Energía</th>" in source
    assert '<td data-label="Estado">' in source
    assert '<td data-label="Medidor"' in source
    assert "<h2>Sesiones de carga</h2>" in source
    assert '<p class="note" id="transaction-history-help">' not in source
    assert '<th>Inicio</th>' in source  # hidden in tablet/mobile
    assert ".transactions-panel th:nth-child(1),.transactions-panel tbody td:nth-child(1)," in source
    assert ".transactions-panel th:nth-child(7),.transactions-panel tbody td:nth-child(7)," in source
    assert "Mostrando {{ ((tx_page - 1) * 10 + 1)" in source
    assert '<td data-label="Cargador"' in source  # retained for mobile/desktop


def test_transaction_detail_removes_only_redundant_explanations():
    from importlib.resources import files

    source = files("ocpp_reports").joinpath("templates/transaction_detail.html.j2").read_text()
    assert "Cobertura: intervalo de eventos observado para la transacción" not in source
    assert 'class="detail-disclaimer"' not in source
    assert "Discontinuidad del medidor detectada" in source
    assert "Vista parcial: se aplicó el límite" in source
    assert "medición antigua / no verificada" in source
    assert 'id="transaction-session-data"' in source
