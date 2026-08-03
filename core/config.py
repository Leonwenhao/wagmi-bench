# SPDX-License-Identifier: Apache-2.0
"""Runtime episode configuration and virtual-clock primitives.

This module formalizes the fixture-local ``episode_config/v1`` shape in typed
runtime code.  It deliberately does not add or alter a public contract schema:
the replay-facing representation is the frozen ``bundle_manifest.run_config``
sub-object.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping, TypeGuard

WEEK_MS: Final = 604_800_000
MAX_REBASED_TIMESTAMP: Final = 99_999_999_999
DEFAULT_STARTING_NAV_MICRO: Final = 10_000_000_000
DEFAULT_GROSS_LEVERAGE_CAP_LEV_1E4: Final = 30_000
DEFAULT_DRAWDOWN_KILL_SWITCH_1E8: Final = 20_000_000
DEFAULT_RESPONSE_DEADLINE_MS: Final = 120_000
DEFAULT_COST_PROFILES: Final[tuple[str, ...]] = ("primary", "stress_2x")


class ConfigError(ValueError):
    """Raised when runtime configuration is structurally invalid."""


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _required_int(
    value: object,
    field: str,
    *,
    minimum: int,
) -> int:
    if not _is_int(value) or value < minimum:
        raise ConfigError(f"{field} must be an integer >= {minimum}")
    return value


def _optional_int(
    value: object,
    field: str,
    *,
    minimum: int,
) -> int | None:
    if value is None:
        return None
    return _required_int(value, field, minimum=minimum)


@dataclass(frozen=True, slots=True)
class EffectiveLookback:
    """Resolved observation lookback depths."""

    bars: int
    funding_prints: int

    def __post_init__(self) -> None:
        _required_int(self.bars, "bars", minimum=1)
        _required_int(self.funding_prints, "funding_prints", minimum=0)


@dataclass(frozen=True, slots=True)
class EpisodeConfig:
    """Complete deterministic runtime input for one episode.

    ``parse_failure_retries`` is harness behavior carried by the historical
    fixture-local file.  It is intentionally omitted from ``to_run_config``:
    the frozen bundle contract records response events, making replay
    independent of retry policy.
    """

    starting_nav_micro: int = DEFAULT_STARTING_NAV_MICRO
    leverage_cap_gross_lev_1e4: int = DEFAULT_GROSS_LEVERAGE_CAP_LEV_1E4
    drawdown_kill_switch_1e8: int = DEFAULT_DRAWDOWN_KILL_SWITCH_1E8
    lookback_bars: int | None = None
    funding_prints: int | None = None
    response_deadline_ms: int = DEFAULT_RESPONSE_DEADLINE_MS
    seed: int = 0
    cost_profiles: tuple[str, ...] = DEFAULT_COST_PROFILES
    turnover_cap_1e8: int | None = None
    parse_failure_retries: int = 1

    def __post_init__(self) -> None:
        _required_int(self.starting_nav_micro, "starting_nav_micro", minimum=1)
        _required_int(
            self.leverage_cap_gross_lev_1e4,
            "leverage_cap_gross_lev_1e4",
            minimum=1,
        )
        _required_int(
            self.drawdown_kill_switch_1e8,
            "drawdown_kill_switch_1e8",
            minimum=1,
        )
        _optional_int(self.lookback_bars, "lookback_bars", minimum=1)
        _optional_int(self.funding_prints, "funding_prints", minimum=0)
        _required_int(
            self.response_deadline_ms,
            "response_deadline_ms",
            minimum=1,
        )
        _required_int(self.seed, "seed", minimum=0)
        _optional_int(self.turnover_cap_1e8, "turnover_cap_1e8", minimum=0)
        _required_int(
            self.parse_failure_retries,
            "parse_failure_retries",
            minimum=0,
        )
        if not self.cost_profiles:
            raise ConfigError("cost_profiles must contain at least one profile")
        if any(not isinstance(profile, str) or not profile for profile in self.cost_profiles):
            raise ConfigError("cost_profiles must contain non-empty strings")
        if len(set(self.cost_profiles)) != len(self.cost_profiles):
            raise ConfigError("cost_profiles must not contain duplicates")
        missing_profiles = set(DEFAULT_COST_PROFILES) - set(self.cost_profiles)
        if missing_profiles:
            raise ConfigError(
                "V1 requires both cost profiles; missing: "
                + ", ".join(sorted(missing_profiles))
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> EpisodeConfig:
        """Load either the runtime run-config shape or the legacy fixture shape."""

        schema = raw.get("schema")
        if schema is not None and schema != "episode_config/v1":
            raise ConfigError("schema must equal episode_config/v1 when present")

        nested_lookback = raw.get("lookback")
        lookback: Mapping[str, object]
        if nested_lookback is None:
            lookback = {}
        elif isinstance(nested_lookback, Mapping):
            lookback = nested_lookback
        else:
            raise ConfigError("lookback must be an object")

        lookback_bars = raw.get("lookback_bars", lookback.get("bars"))
        funding_prints = raw.get(
            "funding_prints",
            lookback.get("funding_prints"),
        )
        response_deadline_ms = raw.get(
            "response_deadline_ms",
            raw.get("decision_timeout_ms", DEFAULT_RESPONSE_DEADLINE_MS),
        )

        profiles_value = raw.get("cost_profiles")
        if profiles_value is None:
            legacy_profile = raw.get("cost_profile")
            profiles: tuple[str, ...]
            if legacy_profile is None:
                profiles = DEFAULT_COST_PROFILES
            elif legacy_profile in DEFAULT_COST_PROFILES:
                # The fixture's singular field selected which oracle was being
                # hand-inspected.  Runtime MATH-5 always simulates both frozen
                # V1 profiles; do not narrow the economic replay input here.
                profiles = DEFAULT_COST_PROFILES
            else:
                raise ConfigError("cost_profile is not a V1 profile")
        elif (
            isinstance(profiles_value, (list, tuple))
            and all(isinstance(item, str) for item in profiles_value)
        ):
            profiles = tuple(profiles_value)
        else:
            raise ConfigError("cost_profiles must be an array of strings")

        return cls(
            starting_nav_micro=_required_int(
                raw.get("starting_nav_micro", DEFAULT_STARTING_NAV_MICRO),
                "starting_nav_micro",
                minimum=1,
            ),
            leverage_cap_gross_lev_1e4=_required_int(
                raw.get(
                    "leverage_cap_gross_lev_1e4",
                    DEFAULT_GROSS_LEVERAGE_CAP_LEV_1E4,
                ),
                "leverage_cap_gross_lev_1e4",
                minimum=1,
            ),
            drawdown_kill_switch_1e8=_required_int(
                raw.get(
                    "drawdown_kill_switch_1e8",
                    DEFAULT_DRAWDOWN_KILL_SWITCH_1E8,
                ),
                "drawdown_kill_switch_1e8",
                minimum=1,
            ),
            lookback_bars=_optional_int(
                lookback_bars,
                "lookback_bars",
                minimum=1,
            ),
            funding_prints=_optional_int(
                funding_prints,
                "funding_prints",
                minimum=0,
            ),
            response_deadline_ms=_required_int(
                response_deadline_ms,
                "response_deadline_ms",
                minimum=1,
            ),
            seed=_required_int(raw.get("seed", 0), "seed", minimum=0),
            cost_profiles=profiles,
            turnover_cap_1e8=_optional_int(
                raw.get("turnover_cap_1e8"),
                "turnover_cap_1e8",
                minimum=0,
            ),
            parse_failure_retries=_required_int(
                raw.get("parse_failure_retries", 1),
                "parse_failure_retries",
                minimum=0,
            ),
        )

    def effective_lookback(
        self,
        *,
        pack_bars: int,
        pack_funding_prints: int,
    ) -> EffectiveLookback:
        """Resolve operator overrides against pack recommendations."""

        return EffectiveLookback(
            bars=self.lookback_bars if self.lookback_bars is not None else pack_bars,
            funding_prints=(
                self.funding_prints
                if self.funding_prints is not None
                else pack_funding_prints
            ),
        )

    def validate_cost_profiles(self, available: Mapping[str, int]) -> None:
        """Reject a profile that is not declared by the pack."""

        missing = [name for name in self.cost_profiles if name not in available]
        if missing:
            raise ConfigError(
                "unknown cost profile(s): " + ", ".join(sorted(missing))
            )

    def to_run_config(self) -> dict[str, object]:
        """Return the exact frozen ``bundle_manifest.run_config`` shape."""

        return {
            "lookback_bars": self.lookback_bars,
            "funding_prints": self.funding_prints,
            "response_deadline_ms": self.response_deadline_ms,
            "seed": self.seed,
            "cost_profiles": list(self.cost_profiles),
            "starting_nav_micro": self.starting_nav_micro,
            "leverage_cap_gross_lev_1e4": self.leverage_cap_gross_lev_1e4,
            "drawdown_kill_switch_1e8": self.drawdown_kill_switch_1e8,
            "turnover_cap_1e8": self.turnover_cap_1e8,
        }


def time_rebase_offset_ms(window_start_ts: int) -> int:
    """Return the largest whole-week multiple not after the pack start."""

    start = _required_int(window_start_ts, "window_start_ts", minimum=0)
    return (start // WEEK_MS) * WEEK_MS


def rebase_timestamp(real_ts: int, *, offset_ms: int) -> int:
    """Map a real pack timestamp to the closed observation time domain."""

    timestamp = _required_int(real_ts, "real_ts", minimum=0)
    offset = _required_int(offset_ms, "offset_ms", minimum=0)
    rebased = timestamp - offset
    if rebased < 0 or rebased > MAX_REBASED_TIMESTAMP:
        raise ConfigError("timestamp falls outside the observation time domain")
    return rebased


@dataclass(frozen=True, slots=True)
class VirtualClock:
    """A deterministic clock driven only by explicit pack timestamps."""

    real_ts: int
    rebase_offset_ms: int

    def __post_init__(self) -> None:
        _required_int(self.real_ts, "real_ts", minimum=0)
        _required_int(self.rebase_offset_ms, "rebase_offset_ms", minimum=0)
        rebase_timestamp(self.real_ts, offset_ms=self.rebase_offset_ms)

    @classmethod
    def for_window(cls, *, window_start_ts: int, real_ts: int) -> VirtualClock:
        return cls(
            real_ts=real_ts,
            rebase_offset_ms=time_rebase_offset_ms(window_start_ts),
        )

    @property
    def rebased_ts(self) -> int:
        return rebase_timestamp(
            self.real_ts,
            offset_ms=self.rebase_offset_ms,
        )

    def advance_to(self, real_ts: int) -> VirtualClock:
        next_ts = _required_int(real_ts, "real_ts", minimum=0)
        if next_ts < self.real_ts:
            raise ConfigError("virtual clock cannot move backwards")
        return VirtualClock(
            real_ts=next_ts,
            rebase_offset_ms=self.rebase_offset_ms,
        )
