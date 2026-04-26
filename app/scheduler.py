"""
Scheduler engine for the Avalon Q Controller.

v2 changes:
- Actions can reference a pool by id: action="pool:<pool_id>".
- A rule carries `reboot_after_pool_switch` (per-rule decision).
- A rule can chain a workmode change with a pool switch.

Action vocabulary:
  off            -> soft-off (standby)
  on             -> soft-on (resume last workmode)
  eco            -> set workmode to Eco
  standard       -> set workmode to Standard
  super          -> set workmode to Super
  none           -> do nothing
  pool:<pool_id> -> switch primary pool to <pool_id>; optional reboot
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

BaseAction = Literal["off", "on", "eco", "standard", "super", "none"]
BASE_ACTIONS: tuple[BaseAction, ...] = ("off", "on", "eco", "standard", "super", "none")

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def is_pool_action(action: str) -> bool:
    return action.startswith("pool:") and len(action) > len("pool:")


def pool_id_from_action(action: str) -> str | None:
    if not is_pool_action(action):
        return None
    return action.split(":", 1)[1]


def is_valid_action(action: str) -> bool:
    if action in BASE_ACTIONS:
        return True
    return is_pool_action(action)


@dataclass
class Rule:
    id: str
    name: str
    action: str
    days: list[int]
    start: time
    end: time
    enabled: bool = True
    priority: int = 0
    season_start: tuple[int, int] | None = None
    season_end: tuple[int, int] | None = None
    reboot_after_pool_switch: bool = False
    chain_workmode: BaseAction | None = None

    def matches(self, now_local: datetime) -> bool:
        if not self.enabled:
            return False
        if now_local.weekday() not in self.days:
            return False
        if not _time_in_window(now_local.time(), self.start, self.end):
            return False
        if not _date_in_season(now_local.date(), self.season_start, self.season_end):
            return False
        return True


@dataclass
class Schedule:
    timezone: str = "UTC"
    enabled: bool = True
    default_action: str = "none"
    rules: list[Rule] = field(default_factory=list)

    def evaluate(self, now_utc: datetime | None = None) -> tuple[str, Rule | None]:
        if not self.enabled:
            return ("none", None)
        tz = ZoneInfo(self.timezone)
        now_local = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(tz)
        candidates = [r for r in self.rules if r.matches(now_local)]
        if not candidates:
            return (self.default_action, None)
        candidates.sort(key=lambda r: r.priority)
        winner = candidates[-1]
        return (winner.action, winner)


def _time_in_window(t: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def _date_in_season(
    d: date,
    season_start: tuple[int, int] | None,
    season_end: tuple[int, int] | None,
) -> bool:
    if season_start is None or season_end is None:
        return True
    sm, sd = season_start
    em, ed = season_end
    md = (d.month, d.day)
    start = (sm, sd)
    end = (em, ed)
    if start <= end:
        return start <= md <= end
    return md >= start or md <= end


def rule_from_dict(d: dict) -> Rule:
    return Rule(
        id=d["id"],
        name=d.get("name", d["id"]),
        action=d["action"],
        days=[int(x) for x in d.get("days", [])],
        start=_parse_hhmm(d["start"]),
        end=_parse_hhmm(d["end"]),
        enabled=bool(d.get("enabled", True)),
        priority=int(d.get("priority", 0)),
        season_start=tuple(d["season_start"]) if d.get("season_start") else None,
        season_end=tuple(d["season_end"]) if d.get("season_end") else None,
        reboot_after_pool_switch=bool(d.get("reboot_after_pool_switch", False)),
        chain_workmode=d.get("chain_workmode") or None,
    )


def rule_to_dict(r: Rule) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "action": r.action,
        "days": list(r.days),
        "start": _format_hhmm(r.start),
        "end": _format_hhmm(r.end),
        "enabled": r.enabled,
        "priority": r.priority,
        "season_start": list(r.season_start) if r.season_start else None,
        "season_end": list(r.season_end) if r.season_end else None,
        "reboot_after_pool_switch": r.reboot_after_pool_switch,
        "chain_workmode": r.chain_workmode,
    }


def schedule_from_dict(d: dict) -> Schedule:
    return Schedule(
        timezone=d.get("timezone", "UTC"),
        enabled=bool(d.get("enabled", True)),
        default_action=d.get("default_action", "none"),
        rules=[rule_from_dict(r) for r in d.get("rules", [])],
    )


def schedule_to_dict(s: Schedule) -> dict:
    return {
        "timezone": s.timezone,
        "enabled": s.enabled,
        "default_action": s.default_action,
        "rules": [rule_to_dict(r) for r in s.rules],
    }


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m))


def _format_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"
