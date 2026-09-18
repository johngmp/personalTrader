import pytest
import yaml
from pydantic import ValidationError

from trader.config import PROJECT_ROOT, RiskConfig, load_settings
from trader.models import AssetClass


def test_project_config_loads_and_is_paper():
    s = load_settings(PROJECT_ROOT)
    assert s.risk.mode == "paper"
    assert s.sleeves["TEST"].enabled
    assert s.asset_class("BTC/USD") is AssetClass.CRYPTO
    assert s.asset_class("SPY") is AssetClass.US_EQUITY
    assert s.asset_class("DOGE/USD") is None


def test_typo_in_risk_yaml_fails_loudly():
    with pytest.raises(ValidationError):
        RiskConfig(account={"max_daily_loss_pct": 3, "max_dayly_loss": 5})


def _write_config(root, sleeves=None, universe=None):
    cfg = root / "config"
    cfg.mkdir()
    for name in ("risk", "costs"):
        (cfg / f"{name}.yaml").write_text((PROJECT_ROOT / "config" / f"{name}.yaml").read_text())
    (cfg / "universe.yaml").write_text(yaml.safe_dump(universe or {"symbols": {"SPY": "us_equity"}}))
    (cfg / "sleeves.yaml").write_text(yaml.safe_dump({"sleeves": sleeves or []}))


def test_sleeve_with_symbol_outside_universe_is_rejected(tmp_path):
    _write_config(tmp_path, sleeves=[{"id": "X", "name": "x", "capital_usd": 1000, "symbols": ["QQQ"]}])
    with pytest.raises(ValidationError, match="missing from universe"):
        load_settings(tmp_path)


def test_duplicate_sleeve_ids_are_rejected(tmp_path):
    sleeve = {"id": "X", "name": "x", "capital_usd": 1000, "symbols": ["SPY"]}
    _write_config(tmp_path, sleeves=[sleeve, sleeve])
    with pytest.raises(ValueError, match="duplicate sleeve ids"):
        load_settings(tmp_path)


def test_correlation_group_with_unknown_symbol_is_rejected(tmp_path):
    _write_config(tmp_path, universe={"symbols": {"SPY": "us_equity"}, "correlation_groups": {"g": ["SPY", "QQQ"]}})
    with pytest.raises(ValidationError, match="unknown symbols"):
        load_settings(tmp_path)
