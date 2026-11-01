"""Settings loaded from environment variables (prefix ``DSB_``)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when a setting is missing or invalid."""


def _get_float(env: Mapping[str, str], key: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _get_int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _get_str(env: Mapping[str, str], key: str, default: str = "") -> str:
    return env.get(key, default).strip()


@dataclass(frozen=True)
class Settings:
    """All runtime settings. See docs/SPEC.md for the meaning of each."""

    dexscreener_base_url: str = "https://api.dexscreener.com"
    rugcheck_base_url: str = "https://api.rugcheck.xyz"
    telegram_base_url: str = "https://api.telegram.org"
    telegram_token: str = ""
    telegram_chat_id: str = ""
    database_path: str = "dexsignal.db"
    interval_seconds: int = 300
    batch_size: int = 30
    max_requests_per_minute: int = 50
    http_timeout_seconds: float = 10.0
    min_liquidity_usd: float = 50_000.0
    max_volume_to_liquidity: float = 50.0
    rugcheck_max_score: int = 50
    screen_ttl_minutes: int = 60
    window_minutes: int = 30
    min_points: int = 3
    rise_pct: float = 100.0
    fall_pct: float = 50.0
    liq_drop_pct: float = 50.0
    alert_cooldown_minutes: int = 360
    retention_days: int = 30

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e: Mapping[str, str] = os.environ if env is None else env
        d = cls()
        settings = cls(
            dexscreener_base_url=_get_str(e, "DSB_DEXSCREENER_BASE_URL", d.dexscreener_base_url).rstrip("/"),
            rugcheck_base_url=_get_str(e, "DSB_RUGCHECK_BASE_URL", d.rugcheck_base_url).rstrip("/"),
            telegram_base_url=_get_str(e, "DSB_TELEGRAM_BASE_URL", d.telegram_base_url).rstrip("/"),
            telegram_token=_get_str(e, "DSB_TELEGRAM_TOKEN"),
            telegram_chat_id=_get_str(e, "DSB_TELEGRAM_CHAT_ID"),
            database_path=_get_str(e, "DSB_DATABASE_PATH", d.database_path),
            interval_seconds=_get_int(e, "DSB_INTERVAL_SECONDS", d.interval_seconds, minimum=10),
            batch_size=_get_int(e, "DSB_BATCH_SIZE", d.batch_size, minimum=1),
            max_requests_per_minute=_get_int(
                e, "DSB_MAX_REQUESTS_PER_MINUTE", d.max_requests_per_minute, minimum=1
            ),
            http_timeout_seconds=_get_float(
                e, "DSB_HTTP_TIMEOUT_SECONDS", d.http_timeout_seconds, minimum=1.0
            ),
            min_liquidity_usd=_get_float(e, "DSB_MIN_LIQUIDITY_USD", d.min_liquidity_usd),
            max_volume_to_liquidity=_get_float(
                e, "DSB_MAX_VOLUME_TO_LIQUIDITY", d.max_volume_to_liquidity, minimum=1.0
            ),
            rugcheck_max_score=_get_int(e, "DSB_RUGCHECK_MAX_SCORE", d.rugcheck_max_score),
            screen_ttl_minutes=_get_int(e, "DSB_SCREEN_TTL_MINUTES", d.screen_ttl_minutes),
            window_minutes=_get_int(e, "DSB_WINDOW_MINUTES", d.window_minutes, minimum=1),
            min_points=_get_int(e, "DSB_MIN_POINTS", d.min_points, minimum=2),
            rise_pct=_get_float(e, "DSB_RISE_PCT", d.rise_pct),
            fall_pct=_get_float(e, "DSB_FALL_PCT", d.fall_pct),
            liq_drop_pct=_get_float(e, "DSB_LIQ_DROP_PCT", d.liq_drop_pct),
            alert_cooldown_minutes=_get_int(e, "DSB_ALERT_COOLDOWN_MINUTES", d.alert_cooldown_minutes),
            retention_days=_get_int(e, "DSB_RETENTION_DAYS", d.retention_days, minimum=1),
        )
        if bool(settings.telegram_token) != bool(settings.telegram_chat_id):
            raise ConfigError("Set both DSB_TELEGRAM_TOKEN and DSB_TELEGRAM_CHAT_ID, or neither")
        if settings.fall_pct > 100 or settings.liq_drop_pct > 100:
            raise ConfigError("DSB_FALL_PCT and DSB_LIQ_DROP_PCT must be <= 100")
        return settings
