"""H0 readiness requires reviewed provenance and refuses inferred units/timezones."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "h0_readiness.py"
spec = importlib.util.spec_from_file_location("h0_readiness", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_readiness_fail_closed_and_nested_lineage():
    a = {"sha256": "a", "artifact": "schema2.bak", "user_version": 2,
         "integrity": "ok", "tables": {"events": {"row_count": 5}}}
    b = {"sha256": "b", "artifact": "schema3.bak", "user_version": 3,
         "integrity": "ok", "tables": {"events": {"row_count": 10}}}
    inv = {"artifacts": [a, b]}
    fixtures = [{"artifact_sha256": "a", "user_version": 2, "rows": {"events": [{}]}}]
    assert not mod.evaluate(inv, fixtures, {})["ready"]
    policy = {"sources": {
        x: {"lineage_id": "same-appliance", "timezone": "UTC", "energy_unit": "kWh",
            "evidence": "reviewed historical schema and field manifest"}
        for x in ("a", "b")
    }}
    result = mod.evaluate(inv, fixtures, policy)
    assert result["ready"]
    assert result["lineages"]["same-appliance"]["requires_record_level_dedup"]
    policy["sources"]["a"]["energy_unit"] = "unknown"
    assert not mod.evaluate(inv, fixtures, policy)["ready"]


def test_reject_unmatched_fixtures_and_unknown_signature():
    inv = {"artifacts": [{"sha256": "a", "artifact": "bad.db", "user_version": 1,
                           "integrity": "ok", "tables": {}}]}
    result = mod.evaluate(inv, [{"artifact_sha256": "b", "rows": {}}], {})
    assert not result["ready"]
    assert any("unrecognized source" in s for s in result["findings"])
