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
    assert ".connectors-panel tbody tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in mobile
    assert ".connectors-panel td{display:flex;flex-direction:column;" in mobile
    assert ".connectors-panel td.empty{grid-column:1/-1}" in mobile
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
    assert "overflow-wrap:anywhere;font-size:.98rem" in mobile
    assert ".connectors-panel td::before{flex:0 0 auto;width:100%;font-size:.82rem}" in mobile
    assert '.connectors-panel td[data-label="Potencia"] .connector-power{display:inline-flex;' in mobile
    assert "white-space:nowrap" in mobile
    assert '.connectors-panel td[data-label="Potencia"] .connector-power .note{font-size:.72rem}' in mobile
    cell = source.split('<td data-label="Potencia" class="numeric">', 1)[1].split("</td>", 1)[0]
    assert cell.startswith('<span class="connector-power">')
    assert cell.endswith("</span>")
    assert ">nominal</span>" in cell
    assert ">medida</span>" in cell
    assert "c.power_fresh" in cell and "c.rated_power_w" in cell
