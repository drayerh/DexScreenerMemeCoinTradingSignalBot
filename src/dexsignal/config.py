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


def _get_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if raw == "":
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true or false, got {raw!r}")


def _get_list(env: Mapping[str, str], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


DISCOVERY_FEEDS = ("profiles", "boosts_latest", "boosts_top")


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
    # Risk intelligence (SPEC FR-12 to FR-17)
    goplus_base_url: str = "https://api.gopluslabs.io"
    goplus_token: str = ""
    goplus_enabled: bool = True
    etherscan_base_url: str = "https://api.etherscan.io"
    etherscan_api_key: str = ""
    pocket_universe_base_url: str = ""
    pocket_universe_api_key: str = ""
    pocket_universe_method: str = "GET"
    pocket_universe_param: str = "address"
    strict_screening: bool = False
    max_tax_pct: float = 10.0
    max_top10_pct: float = 50.0
    deployer_max_rugs: int = 2
    rug_liq_drop_pct: float = 80.0
    track_hours: int = 24
    negative_ttl_minutes: int = 10
    log_format: str = "text"
    # Discovery and watchlist (SPEC FR-1, FR-19)
    discovery_feeds: tuple[str, ...] = ("profiles", "boosts_latest", "boosts_top")
    watch_hours: int = 6
    watch_max_tokens: int = 1000

    @property
    def pocket_universe_enabled(self) -> bool:
        return bool(self.pocket_universe_base_url and self.pocket_universe_api_key)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in logs (REVIEW finding 12)."""
        values = (
            self.telegram_token,
            self.goplus_token,
            self.etherscan_api_key,
            self.pocket_universe_api_key,
        )
        return tuple(v for v in values if len(v) >= 4)

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
            retention_days=_get_int(e, "DSB_RETENTION_DAYS", d.retention_days, minimum=2),
            goplus_base_url=_get_str(e, "DSB_GOPLUS_BASE_URL", d.goplus_base_url).rstrip("/"),
            goplus_token=_get_str(e, "DSB_GOPLUS_TOKEN"),
            goplus_enabled=_get_bool(e, "DSB_GOPLUS_ENABLED", d.goplus_enabled),
            etherscan_base_url=_get_str(e, "DSB_ETHERSCAN_BASE_URL", d.etherscan_base_url).rstrip("/"),
            etherscan_api_key=_get_str(e, "DSB_ETHERSCAN_API_KEY"),
            pocket_universe_base_url=_get_str(e, "DSB_POCKET_UNIVERSE_BASE_URL").rstrip("/"),
            pocket_universe_api_key=_get_str(e, "DSB_POCKET_UNIVERSE_API_KEY"),
            pocket_universe_method=_get_str(
                e, "DSB_POCKET_UNIVERSE_METHOD", d.pocket_universe_method
            ).upper(),
            pocket_universe_param=_get_str(e, "DSB_POCKET_UNIVERSE_PARAM", d.pocket_universe_param),
            strict_screening=_get_bool(e, "DSB_STRICT_SCREENING", d.strict_screening),
            max_tax_pct=_get_float(e, "DSB_MAX_TAX_PCT", d.max_tax_pct),
            max_top10_pct=_get_float(e, "DSB_MAX_TOP10_PCT", d.max_top10_pct),
            deployer_max_rugs=_get_int(e, "DSB_DEPLOYER_MAX_RUGS", d.deployer_max_rugs, minimum=1),
            rug_liq_drop_pct=_get_float(e, "DSB_RUG_LIQ_DROP_PCT", d.rug_liq_drop_pct),
            track_hours=_get_int(e, "DSB_TRACK_HOURS", d.track_hours, minimum=1),
            negative_ttl_minutes=_get_int(e, "DSB_NEGATIVE_TTL_MINUTES", d.negative_ttl_minutes, minimum=1),
            log_format=_get_str(e, "DSB_LOG_FORMAT", d.log_format).lower(),
            discovery_feeds=_get_list(e, "DSB_DISCOVERY_FEEDS", d.discovery_feeds),
            watch_hours=_get_int(e, "DSB_WATCH_HOURS", d.watch_hours),
            watch_max_tokens=_get_int(e, "DSB_WATCH_MAX_TOKENS", d.watch_max_tokens, minimum=1),
        )
        unknown = set(settings.discovery_feeds) - set(DISCOVERY_FEEDS)
        if unknown or not settings.discovery_feeds:
            raise ConfigError(
                f"DSB_DISCOVERY_FEEDS must be a comma-separated subset of {', '.join(DISCOVERY_FEEDS)}"
            )
        if bool(settings.telegram_token) != bool(settings.telegram_chat_id):
            raise ConfigError("Set both DSB_TELEGRAM_TOKEN and DSB_TELEGRAM_CHAT_ID, or neither")
        for key, value in (
            ("DSB_FALL_PCT", settings.fall_pct),
            ("DSB_LIQ_DROP_PCT", settings.liq_drop_pct),
            ("DSB_RUG_LIQ_DROP_PCT", settings.rug_liq_drop_pct),
            ("DSB_MAX_TAX_PCT", settings.max_tax_pct),
            ("DSB_MAX_TOP10_PCT", settings.max_top10_pct),
        ):
            if value > 100:
                raise ConfigError(f"{key} must be <= 100")
        if settings.pocket_universe_method not in {"GET", "POST"}:
            raise ConfigError("DSB_POCKET_UNIVERSE_METHOD must be GET or POST")
        if settings.log_format not in {"text", "json"}:
            raise ConfigError("DSB_LOG_FORMAT must be text or json")
        if bool(settings.pocket_universe_base_url) != bool(settings.pocket_universe_api_key):
            raise ConfigError(
                "Set both DSB_POCKET_UNIVERSE_BASE_URL and DSB_POCKET_UNIVERSE_API_KEY, or neither"
            )
        return settings
