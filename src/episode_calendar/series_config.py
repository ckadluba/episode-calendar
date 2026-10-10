"""Read the configured platforms and active series."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from episode_calendar.config import get_settings


@dataclass(frozen=True)
class ConfiguredPlatform:
    identifier: str
    name: str
    run_import: bool
    display: bool
    has_prereleases: bool
    series: tuple[tuple[str, bool, bool], ...]


def _load_series_config() -> Mapping[str, object]:
    path = Path(get_settings().series_config_path)
    try:
        with path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read series configuration {path}") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"Series configuration {path} must contain a JSON object")
    return config


def configured_platform(provider: str) -> ConfiguredPlatform:
    """Load one platform and all its series settings from the JSON config."""

    platforms = _load_series_config().get("platforms")
    if not isinstance(platforms, list):
        raise RuntimeError("Series configuration must contain a platforms list")

    for raw_platform in platforms:
        if not isinstance(raw_platform, Mapping):
            raise RuntimeError("Series configuration platforms must be objects")
        platform_id = raw_platform.get("id")
        name = raw_platform.get("name")
        if not isinstance(platform_id, str) or not platform_id.strip():
            raise RuntimeError("Series configuration platforms must contain a string id")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"Series configuration platform {platform_id!r} needs a name")
        if platform_id.strip() != provider:
            continue

        run_import = raw_platform.get("run_import", True)
        display = raw_platform.get("display", True)
        if not isinstance(run_import, bool) or not isinstance(display, bool):
            raise RuntimeError(
                f"Series configuration platform {provider!r} needs boolean run_import and display"
            )
        has_prereleases = raw_platform.get("hasPrereleases", True)
        if not isinstance(has_prereleases, bool):
            raise RuntimeError(
                f"Series configuration platform {provider!r} needs a boolean hasPrereleases"
            )
        identifiers = raw_platform.get("series")
        if not isinstance(identifiers, list):
            raise RuntimeError(f"Series configuration platform {provider!r} needs a series list")
        return ConfiguredPlatform(
            identifier=provider,
            name=name.strip(),
            run_import=run_import,
            display=display,
            has_prereleases=has_prereleases,
            series=_configured_series(provider, identifiers),
        )

    raise RuntimeError(f"Series configuration has no platform {provider!r}")


def _configured_series(
    provider: str, identifiers: list[object]
) -> tuple[tuple[str, bool, bool], ...]:
    normalized: list[tuple[str, bool, bool]] = []
    for identifier in identifiers:
        if isinstance(identifier, str):
            value = identifier.strip()
            run_import = True
            display = True
        elif isinstance(identifier, Mapping):
            value = identifier.get("id", "")
            if not isinstance(value, str):
                raise RuntimeError(
                    f"Series configuration entry {provider!r} objects must contain a string id"
                )
            value = value.strip()
            run_import = identifier.get("run_import", True)
            display = identifier.get("display", True)
            if not isinstance(run_import, bool) or not isinstance(display, bool):
                raise RuntimeError(
                    f"Series configuration entry {provider!r} needs boolean run_import and display"
                )
        else:
            raise RuntimeError(
                f"Series configuration entry {provider!r} must contain objects with string ids"
            )
        if value:
            normalized.append((value, run_import, display))
    return tuple(normalized)


def configured_platforms() -> tuple[ConfiguredPlatform, ...]:
    """Return every platform from the JSON config in declaration order."""

    platforms = _load_series_config().get("platforms")
    if not isinstance(platforms, list):
        raise RuntimeError("Series configuration must contain a platforms list")
    result: list[ConfiguredPlatform] = []
    for raw_platform in platforms:
        if not isinstance(raw_platform, Mapping):
            raise RuntimeError("Series configuration platforms must be objects")
        platform_id = raw_platform.get("id")
        if not isinstance(platform_id, str) or not platform_id.strip():
            raise RuntimeError("Series configuration platforms must contain a string id")
        result.append(configured_platform(platform_id.strip()))
    return tuple(result)


def platform_has_prereleases(provider: str) -> bool:
    """Return whether a platform publishes prereleases, defaulting to True when unknown."""

    try:
        return configured_platform(provider).has_prereleases
    except RuntimeError:
        return True


def has_prereleases_by_platform() -> dict[str, bool]:
    """Map platform identifiers to whether they publish prerelease releases."""

    return {platform.identifier: platform.has_prereleases for platform in configured_platforms()}


def configured_series(provider: str) -> tuple[str, ...]:
    """Return active series identifiers for a platform."""

    platform = configured_platform(provider)
    if not platform.run_import:
        return ()
    return tuple(identifier for identifier, run_import, _ in platform.series if run_import)


def displayable_series_by_platform() -> dict[str, set[str] | None]:
    """Return displayable series IDs, or ``None`` when all are displayable."""

    platforms = _load_series_config().get("platforms")
    if not isinstance(platforms, list):
        raise RuntimeError("Series configuration must contain a platforms list")
    result: dict[str, set[str] | None] = {}
    for raw_platform in platforms:
        if not isinstance(raw_platform, Mapping):
            raise RuntimeError("Series configuration platforms must be objects")
        platform_id = raw_platform.get("id")
        if not isinstance(platform_id, str):
            raise RuntimeError("Series configuration platforms must contain a string id")
        platform = configured_platform(platform_id)
        if platform.display:
            hidden_series = any(not display for _, _, display in platform.series)
            result[platform.identifier] = (
                {identifier for identifier, _, display in platform.series if display}
                if hidden_series
                else None
            )
    return result
