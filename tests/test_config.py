import logging

import pytest

from discord_quota.config import (DEFAULT_TIMEZONE, DEFAULT_WARN_THRESHOLD, Limits,
                                  is_active, is_blocked, is_unlimited, load_config,
                                  parse_config)

FULL = {
    "enabled": True,
    "default": {"daily_usd": 1.0, "weekly_usd": 5.0},
    "users": {
        535154433869217813: {"daily_usd": -1, "weekly_usd": -1},
        "42": {"daily_usd": 3.0},
    },
}


def test_defaults():
    c = parse_config({"enabled": True, "default": {"daily_usd": 1, "weekly_usd": 2}})
    assert c.enabled and c.timezone == DEFAULT_TIMEZONE == "Europe/Stockholm"
    assert c.warn_threshold == DEFAULT_WARN_THRESHOLD == 0.90
    assert c.limits_for("1") == (1.0, 2.0)


def test_semantics_helpers():
    assert is_unlimited(-1.0) and not is_unlimited(0)
    assert is_blocked(0.0) and not is_blocked(-1)
    assert is_active(0.01) and not is_active(0)
    lim = Limits(-1, 0)
    assert lim.daily_unlimited and lim.weekly_blocked
    assert not lim.daily_active and not lim.weekly_active
    assert Limits(1, 2).weekly_active


def test_override_supersedes_and_omitted_is_zero():
    c = parse_config(FULL)
    assert c.limits_for("42") == (3.0, 0.0)
    assert c.limits_for("other") == (1.0, 5.0)


def test_int_user_ids_matched_as_strings():
    c = parse_config(FULL)
    assert c.limits_for("535154433869217813") == (-1.0, -1.0)
    assert c.limits_for(535154433869217813) == (-1.0, -1.0)
    assert c.limits_for(42) == (3.0, 0.0)


def test_missing_section_or_disabled():
    assert not parse_config(None).enabled
    assert not parse_config({**FULL, "enabled": False}).enabled


def test_no_default_is_blocked():
    assert parse_config({"enabled": True}).limits_for("1") == (0.0, 0.0)


@pytest.mark.parametrize("bad", ["abc", [1], True, float("nan"), float("inf"), {"a": 1}])
def test_malformed_limit_fails_closed_with_warning(bad, caplog):
    with caplog.at_level(logging.WARNING):
        c = parse_config({"default": {"daily_usd": bad, "weekly_usd": 5}})
    assert c.limits_for("1") == (0.0, 5.0)
    assert "discord_quota" in caplog.text


def test_malformed_sections(caplog):
    with caplog.at_level(logging.WARNING):
        c = parse_config("oops")
    assert c.enabled and c.limits_for("1") == (0.0, 0.0)
    c = parse_config({"default": "x", "users": {"1": 5}, "warn_threshold": "bad",
                      "timezone": "Nope/Zone", "enabled": "maybe"})
    assert c.enabled and c.limits_for("1") == (0.0, 0.0) and c.limits_for("2") == (0.0, 0.0)
    assert c.warn_threshold == 0.90 and c.timezone == DEFAULT_TIMEZONE
    assert caplog.text.count("WARNING") >= 5


def test_load_from_path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text('discord_quota:\n  default: {daily_usd: 2, weekly_usd: 4}\n'
                 '  users:\n    123456789012345678: {daily_usd: 9, weekly_usd: 9}\n'
                 '  warn_threshold: 0.5\n  timezone: UTC\n')
    c = load_config(p)
    assert c.limits_for("123456789012345678") == (9.0, 9.0)
    assert c.limits_for("x") == (2.0, 4.0)
    assert c.warn_threshold == 0.5 and c.timezone == "UTC"


def test_load_path_missing_file_or_section(tmp_path):
    assert not load_config(tmp_path / "nope.yaml").enabled
    p = tmp_path / "c.yaml"
    p.write_text("other: 1\n")
    assert not load_config(p).enabled


def test_load_invalid_yaml_fails_closed(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("a: [unclosed\n")
    c = load_config(p)
    assert c.enabled and c.limits_for("1") == (0.0, 0.0)
