from activities import (
    MAX_CALCULATE_EXPR_LENGTH,
    _calculate_guardrail_violation,
    _load_config,
    _normalize,
    _weather_guardrail_violation,
)


def test_normalize_strips_accents_and_case():
    assert _normalize("Coréia Do Norte") == "coreia do norte"
    assert _normalize("  PyongYang  ") == "pyongyang"


def test_weather_guardrail_blocks_known_locations():
    assert _weather_guardrail_violation({"city": "Coreia do Norte"}) is not None
    assert _weather_guardrail_violation({"city": "Pyongyang"}) is not None
    assert _weather_guardrail_violation({"city": "DPRK"}) is not None


def test_weather_guardrail_allows_other_cities():
    assert _weather_guardrail_violation({"city": "Lisboa"}) is None
    assert _weather_guardrail_violation({"city": "Sao Paulo"}) is None


def test_calculate_guardrail_blocks_long_expression():
    expr = "1+" * (MAX_CALCULATE_EXPR_LENGTH // 2 + 5)
    violation = _calculate_guardrail_violation({"expression": expr})
    assert violation is not None
    assert "excede o limite" in violation


def test_calculate_guardrail_allows_short_expression():
    assert _calculate_guardrail_violation({"expression": "(12 + 8) * 3"}) is None


def test_config_falls_back_to_defaults_when_file_missing(tmp_path, monkeypatch):
    import activities

    monkeypatch.setattr(activities, "_CONFIG_PATH", tmp_path / "missing.json")
    config = _load_config()
    assert "coreia do norte" in config["blocked_weather_locations"]
    assert config["max_calculate_expr_length"] == 200
    assert config["max_session_cost_usd"] == 0.50
