from ocpp_reports.warehouse.queries import _add_connector_zero_energy_rollups


def test_connector_zero_is_sum_of_individuals():
    rows = [
        {'source_id': 's', 'charger_id': 'a', 'connector_id': 0, 'energy_wh': 999},
        {'source_id': 's', 'charger_id': 'a', 'connector_id': 1, 'energy_wh': 1677350},
        {'source_id': 's', 'charger_id': 'a', 'connector_id': 2, 'energy_wh': None},
        {'source_id': 's', 'charger_id': 'b', 'connector_id': 0, 'energy_wh': None},
        {'source_id': 's', 'charger_id': 'b', 'connector_id': 1, 'energy_wh': 1000},
        {'source_id': 'other', 'charger_id': 'a', 'connector_id': 0, 'energy_wh': None},
        {'source_id': 'other', 'charger_id': 'a', 'connector_id': 1, 'energy_wh': 500},
    ]
    _add_connector_zero_energy_rollups(rows)
    assert [r['aggregate_energy_wh'] for r in rows if r['connector_id'] == 0] == [1677350, 1000, 500]
    assert rows[0]['energy_wh'] == 999
    assert 'aggregate_energy_wh' not in rows[1]
