import pytest
from ocpp_reports.collector_config import load_collector_config


def test_collector_toml_credentials(tmp_path):
    path = tmp_path / "collector.toml"
    path.write_text('[sources]\n"sat-001" = "token-001"\n"sat-002" = "token-002"\n')
    assert load_collector_config(path) == {"sat-001": "token-001", "sat-002": "token-002"}


@pytest.mark.parametrize("content", ["", "[sources]\n", '[sources]\na = ""\n',
                                     '[sources]\na = 123\n', '[wrong]\na = "token"\n'])
def test_collector_rejects_missing_or_invalid_tokens(tmp_path, content):
    path = tmp_path / "collector.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_collector_config(path)
