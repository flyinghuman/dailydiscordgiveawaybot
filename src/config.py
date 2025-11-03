from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional

import os
import yaml


class ConfigError(RuntimeError):
    """Raised when the configuration file is invalid."""


@dataclass(slots=True)
class LoggingConfig:
    level: str
    logger_channel_id: Optional[int]


@dataclass(slots=True)
class ManualDefaults:
    duration_minutes: int = 1440


@dataclass(slots=True)
class ScheduledGiveawayConfig:
    id: str
    enabled: bool
    channel_id: Optional[int]
    winners: int
    title: str
    description: str
    start_time: time
    end_time: time

    @property
    def duration_minutes(self) -> int:
        start_minutes = self.start_time.hour * 60 + self.start_time.minute
        end_minutes = self.end_time.hour * 60 + self.end_time.minute
        if end_minutes <= start_minutes:
            # assume end time is next day
            end_minutes += 24 * 60
        return end_minutes - start_minutes


@dataclass(slots=True)
class SchedulingConfig:
    auto_enabled: bool
    giveaways: List[ScheduledGiveawayConfig]


@dataclass(slots=True)
class PermissionsConfig:
    admin_roles: List[int]
    development_guild_id: Optional[int] = None


@dataclass(slots=True)
class Config:
    token: str
    application_id: int
    default_timezone: str
    logging: LoggingConfig
    manual_defaults: ManualDefaults
    scheduling: SchedulingConfig
    permissions: PermissionsConfig


def _require(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required config key: {key}")
    return data[key]

def _resolve_env_value(value: str, key: str) -> str:
    trimmed = value.strip()
    if trimmed.startswith("${") and trimmed.endswith("}"):
        env_name = trimmed[2:-1].strip()
        if not env_name:
            raise ConfigError(f"Environment reference for '{key}' is empty.")
        env_value = os.getenv(env_name)
        if env_value is None:
            raise ConfigError(
                f"Environment variable '{env_name}' referenced by '{key}' is not set."
            )
        return env_value
    return value

def _parse_time(value: str, key: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ConfigError(
            f"Invalid time format for '{key}': {value!r}. Expected HH:MM (24h)."
        ) from exc


def _parse_logging(data: Dict[str, Any]) -> LoggingConfig:
    level = data.get("level", "INFO")
    logger_channel_id = data.get("logger_channel_id")
    if logger_channel_id is not None and not isinstance(logger_channel_id, int):
        raise ConfigError(
            "logging.logger_channel_id must be an integer channel ID or null."
        )
    return LoggingConfig(level=level, logger_channel_id=logger_channel_id)


def _parse_manual_defaults(data: Dict[str, Any]) -> ManualDefaults:
    duration = data.get("duration_minutes", 1440)
    if not isinstance(duration, int) or duration <= 0:
        raise ConfigError(
            "manual_defaults.duration_minutes must be a positive integer."
        )
    return ManualDefaults(duration_minutes=duration)


def _parse_scheduling(data: Dict[str, Any]) -> SchedulingConfig:
    giveaways_raw = data.get("giveaways", [])
    if not isinstance(giveaways_raw, list):
        raise ConfigError("scheduling.giveaways must be a list.")

    auto_enabled_default = bool(giveaways_raw)
    auto_enabled = bool(data.get("auto_enabled", auto_enabled_default))

    giveaways: List[ScheduledGiveawayConfig] = []
    seen_ids: set[str] = set()
    for entry in giveaways_raw:
        if not isinstance(entry, dict):
            raise ConfigError("Each scheduled giveaway entry must be an object.")

        try:
            giveaway_id = str(_require(entry, "id"))
            enabled = bool(entry.get("enabled", True))
            channel_id_raw = entry.get("channel_id")
            channel_id = None
            if channel_id_raw not in (None, ""):
                channel_id = int(channel_id_raw)
                if channel_id <= 0:
                    raise ValueError
            winners = int(_require(entry, "winners"))
            title = str(_require(entry, "title"))
            description = str(_require(entry, "description"))
            start_time = _parse_time(str(_require(entry, "start_time")), "start_time")
            end_time = _parse_time(str(_require(entry, "end_time")), "end_time")
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"Invalid scheduling entry: {entry}") from exc

        if winners <= 0:
            raise ConfigError(
                f"scheduling.giveaways[{giveaway_id}].winners must be greater than zero."
            )
        if giveaway_id in seen_ids:
            raise ConfigError(
                f"Duplicate scheduled giveaway id detected: {giveaway_id}"
            )
        seen_ids.add(giveaway_id)

        giveaways.append(
            ScheduledGiveawayConfig(
                id=giveaway_id,
                enabled=enabled,
                channel_id=channel_id,
                winners=winners,
                title=title,
                description=description,
                start_time=start_time,
                end_time=end_time,
            )
        )

    return SchedulingConfig(auto_enabled=auto_enabled, giveaways=giveaways)


def _parse_permissions(data: Dict[str, Any]) -> PermissionsConfig:
    admin_roles_raw = data.get("admin_roles", [])
    if not isinstance(admin_roles_raw, list):
        raise ConfigError("permissions.admin_roles must be a list of role IDs.")
    admin_roles: List[int] = []
    for role_id in admin_roles_raw:
        try:
            admin_roles.append(int(role_id))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"permissions.admin_roles contains invalid role id: {role_id!r}"
            ) from exc
    dev_guild_raw = data.get("development_guild_id")
    development_guild_id: Optional[int]
    if dev_guild_raw in (None, "", 0):
        development_guild_id = None
    else:
        try:
            development_guild_id = int(dev_guild_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "permissions.development_guild_id must be an integer guild ID or null."
            ) from exc
        if development_guild_id <= 0:
            raise ConfigError(
                "permissions.development_guild_id must be a positive integer."
            )
    return PermissionsConfig(
        admin_roles=admin_roles, development_guild_id=development_guild_id
    )


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"Config file {path} does not exist.")

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError("Configuration file must contain a mapping at the root.")

    token_raw = str(_require(data, "token"))
    token = _resolve_env_value(token_raw, "token").strip()
    if not token:
        raise ConfigError("token must not be empty.")
    application_id = int(_require(data, "application_id"))
    default_timezone = str(data.get("default_timezone", "UTC"))
    logging_cfg = _parse_logging(data.get("logging", {}))
    manual_defaults = _parse_manual_defaults(data.get("manual_defaults", {}))
    scheduling_cfg = _parse_scheduling(data.get("scheduling", {}))
    permissions_cfg = _parse_permissions(data.get("permissions", {}))

    return Config(
        token=token,
        application_id=application_id,
        default_timezone=default_timezone,
        logging=logging_cfg,
        manual_defaults=manual_defaults,
        scheduling=scheduling_cfg,
        permissions=permissions_cfg,
    )
