"""Typed ``discord_quota`` config loader (PLAN.md section 3).

Value semantics per window: ``v < 0`` unlimited, ``v == 0`` blocked,
``v > 0`` active USD budget. Malformed values fail closed (blocked) and log a
warning. Hermes modules and PyYAML are imported lazily.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Stockholm"
DEFAULT_WARN_THRESHOLD = 0.90


def is_unlimited(v: float) -> bool:
    return v < 0


def is_blocked(v: float) -> bool:
    return v == 0


def is_active(v: float) -> bool:
    return v > 0


@dataclass(frozen=True)
class Limits:
    daily_usd: float = 0.0
    weekly_usd: float = 0.0

    @property
    def daily_unlimited(self) -> bool:
        return is_unlimited(self.daily_usd)

    @property
    def daily_blocked(self) -> bool:
        return is_blocked(self.daily_usd)

    @property
    def daily_active(self) -> bool:
        return is_active(self.daily_usd)

    @property
    def weekly_unlimited(self) -> bool:
        return is_unlimited(self.weekly_usd)

    @property
    def weekly_blocked(self) -> bool:
        return is_blocked(self.weekly_usd)

    @property
    def weekly_active(self) -> bool:
        return is_active(self.weekly_usd)

    def __iter__(self):
        yield self.daily_usd
        yield self.weekly_usd


@dataclass(frozen=True)
class QuotaConfig:
    enabled: bool = False
    timezone: str = DEFAULT_TIMEZONE
    warn_threshold: float = DEFAULT_WARN_THRESHOLD
    default: Limits = field(default_factory=Limits)
    users: Mapping[str, Limits] = field(default_factory=dict)
    free_providers: tuple = ()

    def is_free_provider(self, provider: Any) -> bool:
        """True when calls answered by ``provider`` cost $0 (case-insensitive)."""
        name = str(provider or "").strip().lower()
        return bool(name) and name in self.free_providers

    def limits_for(self, user_id: Any) -> tuple[float, float]:
        """Return ``(daily, weekly)``; a users entry fully supersedes default."""
        lim = self.users.get(str(user_id), self.default)
        return (lim.daily_usd, lim.weekly_usd)

    def limits_obj_for(self, user_id: Any) -> Limits:
        return self.users.get(str(user_id), self.default)


def _limit(value: Any, where: str) -> float:
    """Parse one limit; omitted -> 0.0, malformed -> 0.0 with a warning."""
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        logger.warning("discord_quota: malformed %s=%r; treating as 0.0 (blocked)", where, value)
        return 0.0
    try:
        f = float(value)
    except ValueError:
        logger.warning("discord_quota: malformed %s=%r; treating as 0.0 (blocked)", where, value)
        return 0.0
    if not math.isfinite(f):
        logger.warning("discord_quota: non-finite %s=%r; treating as 0.0 (blocked)", where, value)
        return 0.0
    return f


def _limits(raw: Any, where: str) -> Limits:
    if raw is None:
        return Limits()
    if not isinstance(raw, Mapping):
        logger.warning("discord_quota: %s must be a mapping, got %r; blocked", where, raw)
        return Limits()
    return Limits(_limit(raw.get("daily_usd"), f"{where}.daily_usd"),
                  _limit(raw.get("weekly_usd"), f"{where}.weekly_usd"))


def _timezone(raw: Any) -> str:
    if raw is None:
        return DEFAULT_TIMEZONE
    if isinstance(raw, str) and raw.strip():
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(raw.strip())
            return raw.strip()
        except Exception:
            pass
    logger.warning("discord_quota: invalid timezone %r; using %s", raw, DEFAULT_TIMEZONE)
    return DEFAULT_TIMEZONE


def _threshold(raw: Any) -> float:
    if raw is None:
        return DEFAULT_WARN_THRESHOLD
    if not isinstance(raw, bool) and isinstance(raw, (int, float, str)):
        try:
            f = float(raw)
            if math.isfinite(f) and 0 < f <= 1:
                return f
        except ValueError:
            pass
    logger.warning("discord_quota: invalid warn_threshold %r; using %s",
                   raw, DEFAULT_WARN_THRESHOLD)
    return DEFAULT_WARN_THRESHOLD


def _enabled(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    logger.warning("discord_quota: malformed enabled=%r; staying enabled (fail-closed)", raw)
    return True


def _free_providers(raw: Any) -> tuple:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        logger.warning("discord_quota: free_providers must be a list, got %r; ignored", raw)
        return ()
    out = []
    for item in raw:
        if not isinstance(item, str):
            logger.warning("discord_quota: ignoring non-string free_providers entry %r", item)
            continue
        name = item.strip().lower()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _strict_key(v: float) -> float:
    return math.inf if v < 0 else v


def _stricter(a: Limits, b: Limits) -> Limits:
    """Per window: blocked beats active (lower wins) beats unlimited."""
    pick = lambda x, y: x if _strict_key(x) <= _strict_key(y) else y
    return Limits(pick(a.daily_usd, b.daily_usd), pick(a.weekly_usd, b.weekly_usd))


def parse_config(section: Any) -> QuotaConfig:
    """Build a QuotaConfig from the raw ``discord_quota`` section."""
    if section is None:
        return QuotaConfig(enabled=False)
    if not isinstance(section, Mapping):
        logger.warning("discord_quota: section must be a mapping, got %r; "
                       "enabled with everyone blocked", section)
        return QuotaConfig(enabled=True)
    if not _enabled(section.get("enabled")):
        return QuotaConfig(enabled=False)

    users: dict[str, Limits] = {}
    raw_users = section.get("users")
    if raw_users is not None and not isinstance(raw_users, Mapping):
        logger.warning("discord_quota: users must be a mapping, got %r; ignored", raw_users)
        raw_users = None
    for uid, raw in (raw_users or {}).items():
        new = _limits(raw, f"users.{uid}")
        key = str(uid)
        if key in users:
            new = _stricter(users[key], new)
            logger.warning("discord_quota: duplicate user id %r in users; "
                           "keeping the stricter limits", key)
        users[key] = new

    return QuotaConfig(
        enabled=True,
        timezone=_timezone(section.get("timezone")),
        warn_threshold=_threshold(section.get("warn_threshold")),
        default=_limits(section.get("default"), "default"),
        users=users,
        free_providers=_free_providers(section.get("free_providers")),
    )


def _read_raw(config_path: Path | str | None) -> Any:
    if config_path is not None:
        import yaml
        try:
            data = yaml.safe_load(Path(config_path).read_text()) or {}
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("discord_quota: cannot read %s (%s); failing closed", config_path, exc)
            return {"enabled": True}
        if not isinstance(data, Mapping):
            logger.warning("discord_quota: %s top level is not a mapping; failing closed",
                           config_path)
            return {"enabled": True}
        return data.get("discord_quota")
    from hermes_cli.config import load_config  # lazy: only inside Hermes
    return (load_config() or {}).get("discord_quota")


def load_config(config_path: Path | str | None = None) -> QuotaConfig:
    """Load config via Hermes' loader, or from ``config_path`` when injected."""
    try:
        section = _read_raw(config_path)
    except Exception as exc:
        logger.warning("discord_quota: config load failed (%s); failing closed", exc)
        return QuotaConfig(enabled=True)
    return parse_config(section)
