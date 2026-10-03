"""AI budget guardrails (AI_COST_MODEL.md §7, §7.1, §7.2).

Spend is read from ``ai_cost_rollups`` since 00:00 UTC: the API role may read its own user's
roll-ups (``ai_cost_rollups_api_read``) and the worker role reads them in the user's transaction,
always with an explicit ``user_id`` predicate; only the worker reads every user's roll-ups for the
global budget. Roll-ups lag by up to 15 minutes, so a few calls can pass after a cap is crossed.

``BudgetGuard`` sits inside ``AIClient`` (§7.2): every ``generate`` and ``embed`` call is checked
before the cassette or provider step, so no caller can skip it. A refused call raises
``BudgetExceeded`` (``eca.platform.errors``); the API maps it to 429 with ``Retry-After`` and every
other caller takes its degradation route. Cap values and per-role levels come from
``config/budgets.yaml``.
"""

from __future__ import annotations

import datetime
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select

from eca.intelligence.models import ai_cost_rollups_table
from eca.intelligence.provider.types import ConfigError
from eca.platform.audit import audit_log_table, record_audit
from eca.platform.config import REPO_ROOT, get_settings
from eca.platform.errors import BudgetExceeded
from eca.platform.uow import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger("eca.intelligence.budget")

SPEND_CACHE_S = 60.0


class BudgetLevel(StrEnum):
    OK = "ok"
    SOFT = "soft_cap"  # AI-07 and AI-11 refused; chat answers with AI-06 or deterministically
    HARD = "hard_cap"  # chat deterministic only; background AI paused except VIP/outbound AI-01


_RANK = {BudgetLevel.OK: 0, BudgetLevel.SOFT: 1, BudgetLevel.HARD: 2}


class RolePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_at: Literal["soft", "hard", "never"] = "hard"
    background: bool = False
    exempt_allowed: bool = False
    daily_calls: int | None = Field(default=None, ge=1)

    @property
    def stop_level(self) -> BudgetLevel | None:
        return {"soft": BudgetLevel.SOFT, "hard": BudgetLevel.HARD}.get(self.stop_at)


class _Caps(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    soft: Decimal = Field(gt=0)
    hard: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> _Caps:
        if self.hard < self.soft:
            raise ValueError("the hard cap must not be below the soft cap")
        return self


class MeetingLimits(BaseModel):
    """Meeting upload limits (AI_COST_MODEL.md §7, §7.3), checked after ``ffprobe``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_upload_hours: float = Field(default=3.0, gt=0)
    max_weekly_hours: float = Field(default=10.0, gt=0)


class BudgetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    per_user_daily_usd: _Caps
    global_daily_usd: Decimal | None = Field(default=None, gt=0)
    global_alert_fraction: float = Field(default=0.7, gt=0, le=1)
    vip_min_importance: int = Field(default=4, ge=1, le=5)
    roles: dict[str, RolePolicy] = Field(default_factory=dict)
    meetings: MeetingLimits = Field(default_factory=MeetingLimits)

    @classmethod
    def from_file(cls, path: Path) -> BudgetConfig:
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise ConfigError(f"Invalid budget configuration {path.name}: {exc}") from exc

    def policy(self, role: str) -> RolePolicy:
        """A role missing from the file stops at the hard cap (fail safe, never unbounded)."""
        return self.roles.get(role, RolePolicy())

    def level_for(self, spend: Decimal) -> BudgetLevel:
        if spend >= self.per_user_daily_usd.hard:
            return BudgetLevel.HARD
        if spend >= self.per_user_daily_usd.soft:
            return BudgetLevel.SOFT
        return BudgetLevel.OK


@lru_cache(maxsize=1)
def default_budget_config() -> BudgetConfig:
    """``budgets.yaml`` from ``API_AI_CONFIG_DIR`` (default ``<repository root>/config``)."""
    configured = get_settings().api_ai_config_dir
    directory = Path(configured) if configured else REPO_ROOT / "config"
    return BudgetConfig.from_file(directory / "budgets.yaml")


def level_for(spend: Decimal, config: BudgetConfig | None = None) -> BudgetLevel:
    return (config or default_budget_config()).level_for(spend)


def reached(level: BudgetLevel, stop: BudgetLevel | None) -> bool:
    return stop is not None and _RANK[level] >= _RANK[stop]


def day_start(now: datetime.datetime) -> datetime.datetime:
    return now.astimezone(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def seconds_until_reset(now: datetime.datetime) -> int:
    """Seconds until the spend window resets (next 00:00 UTC), at least 1."""
    reset = day_start(now) + datetime.timedelta(days=1)
    return max(1, int((reset - now).total_seconds()))


def reset_at(now: datetime.datetime) -> datetime.datetime:
    return day_start(now) + datetime.timedelta(days=1)


async def daily_spend(uow: UnitOfWork, *, now: datetime.datetime) -> Decimal:
    if uow.user_id is None:
        return Decimal(0)
    r = ai_cost_rollups_table
    total = (
        await uow.session.execute(
            select(func.coalesce(func.sum(r.c.est_cost_usd), 0)).where(
                r.c.user_id == uow.user_id, r.c.bucket_start >= day_start(now)
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def role_calls_today(uow: UnitOfWork, role: str, *, now: datetime.datetime) -> int:
    if uow.user_id is None:
        return 0
    r = ai_cost_rollups_table
    total = (
        await uow.session.execute(
            select(func.coalesce(func.sum(r.c.calls), 0)).where(
                r.c.user_id == uow.user_id, r.c.role == role, r.c.bucket_start >= day_start(now)
            )
        )
    ).scalar_one()
    return int(total)


async def global_spend(uow: UnitOfWork, *, now: datetime.datetime) -> Decimal:
    """Every user's spend today (worker role only: the API role sees its own roll-ups only)."""
    r = ai_cost_rollups_table
    total = (
        await uow.session.execute(
            select(func.coalesce(func.sum(r.c.est_cost_usd), 0)).where(r.c.bucket_start >= day_start(now))
        )
    ).scalar_one()
    return Decimal(str(total))


async def budget_level(
    uow: UnitOfWork, *, now: datetime.datetime, config: BudgetConfig | None = None
) -> BudgetLevel:
    return level_for(await daily_spend(uow, now=now), config)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@dataclass
class BudgetGuard:
    """Per-call budget check inside ``AIClient`` (§7.2).

    ``global_scope`` is set by the worker composition: only the worker role can read every
    user's roll-ups, and the global budget applies to background roles only. Spend is cached per
    user for ``SPEND_CACHE_S`` seconds in the process, so a burst of calls costs one read.
    """

    uow_factory: UnitOfWorkFactory
    config: BudgetConfig
    global_scope: bool = False
    clock: Callable[[], datetime.datetime] = _utcnow
    monotonic: Callable[[], float] = time.monotonic
    _cache: dict[tuple[str, str], tuple[float, Decimal | int]] = field(default_factory=dict)

    async def check(self, role: str, *, user_id: UUID | None, exempt: bool = False) -> None:
        policy = self.config.policy(role)
        if policy.stop_at == "never":
            return
        now = self.clock()
        if user_id is not None:
            level = self.config.level_for(await self._user_spend(user_id, now))
            if reached(level, policy.stop_level) and not (exempt and policy.exempt_allowed):
                self._refuse(role, level.value, now, user_id)
            if policy.daily_calls is not None:
                calls = await self._role_calls(user_id, role, now)
                if calls >= policy.daily_calls:
                    self._refuse(role, "role_daily_calls", now, user_id)
        cap = self.config.global_daily_usd
        if (
            self.global_scope
            and policy.background
            and cap is not None
            and await self._global_spend(now) >= cap
        ):
            self._refuse(role, "global_budget", now, user_id)

    def _refuse(self, role: str, level: str, now: datetime.datetime, user_id: UUID | None) -> None:
        log.warning("ai_budget_blocked", role=role, level=level, has_user=user_id is not None)
        raise BudgetExceeded(
            "The daily AI budget is used up for this kind of request.",
            retry_after_s=seconds_until_reset(now),
            details={"role": role, "level": level},
        )

    def _cached(self, key: tuple[str, str]) -> Decimal | int | None:
        hit = self._cache.get(key)
        if hit is not None and hit[0] > self.monotonic():
            return hit[1]
        return None

    def _store(self, key: tuple[str, str], value: Decimal | int) -> None:
        self._cache[key] = (self.monotonic() + SPEND_CACHE_S, value)

    async def _user_spend(self, user_id: UUID, now: datetime.datetime) -> Decimal:
        key = ("user", f"{user_id}:{day_start(now).date()}")
        hit = self._cached(key)
        if isinstance(hit, Decimal):
            return hit
        async with self.uow_factory(user_id=user_id) as uow:
            spend = await daily_spend(uow, now=now)
        self._store(key, spend)
        return spend

    async def _role_calls(self, user_id: UUID, role: str, now: datetime.datetime) -> int:
        key = ("calls", f"{user_id}:{role}:{day_start(now).date()}")
        hit = self._cached(key)
        if isinstance(hit, int):
            return hit
        async with self.uow_factory(user_id=user_id) as uow:
            calls = await role_calls_today(uow, role, now=now)
        self._store(key, calls)
        return calls

    async def _global_spend(self, now: datetime.datetime) -> Decimal:
        key = ("global", str(day_start(now).date()))
        hit = self._cached(key)
        if isinstance(hit, Decimal):
            return hit
        async with self.uow_factory(user_id=None) as uow:
            spend = await global_spend(uow, now=now)
        self._store(key, spend)
        return spend


# ---------------------------------------------------------------- audit (budget-cap events)

SOFT_CAP_ACTION = "ai_budget_soft_cap_reached"
HARD_CAP_ACTION = "ai_budget_hard_cap_reached"
GLOBAL_ALERT_ACTION = "ai_global_budget_alert"
GLOBAL_REACHED_ACTION = "ai_global_budget_reached"


def _user_ref(user_id: UUID) -> str:
    """Hashed user reference for logs (BACKEND_DESIGN.md §19)."""
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:12]


async def _audited(uow: UnitOfWork, action: str, date: str, user_id: UUID | None) -> bool:
    a = audit_log_table
    user_match = a.c.user_id.is_(None) if user_id is None else a.c.user_id == user_id
    found = (
        await uow.session.execute(
            select(a.c.id)
            .where(user_match, a.c.action == action, a.c.metadata["date"].astext == date)
            .limit(1)
        )
    ).first()
    return found is not None


async def audit_budget_caps(
    uow_factory: UnitOfWorkFactory, *, now: datetime.datetime, config: BudgetConfig | None = None
) -> int:
    """Worker only, after each roll-up: one ``audit_log`` row per user, cap and UTC day when the
    user's spend first reaches a cap, and one NULL-user row for the global alert and budget
    (TECHNICAL_DESIGN.md §17.7). Hard-cap and global rows are also logged at error level (the
    operator alert of AI_COST_MODEL.md §7). Returns the number of rows written."""
    cfg = config or default_budget_config()
    date = day_start(now).date().isoformat()
    r = ai_cost_rollups_table
    async with uow_factory(user_id=None) as uow:
        rows = (
            await uow.session.execute(
                select(r.c.user_id, func.sum(r.c.est_cost_usd).label("spend"))
                .where(r.c.bucket_start >= day_start(now), r.c.user_id.is_not(None))
                .group_by(r.c.user_id)
                .having(func.sum(r.c.est_cost_usd) >= cfg.per_user_daily_usd.soft)
                .order_by(r.c.user_id)
            )
        ).all()
        total = await global_spend(uow, now=now)
    written = 0
    for row in rows:
        level = cfg.level_for(Decimal(str(row.spend)))
        actions = [SOFT_CAP_ACTION] + ([HARD_CAP_ACTION] if level is BudgetLevel.HARD else [])
        async with uow_factory(user_id=row.user_id) as uow:
            for action in actions:
                if await _audited(uow, action, date, row.user_id):
                    continue
                await record_audit(uow, action, actor="system", metadata={"date": date, "level": level.value})
                written += 1
                if action == HARD_CAP_ACTION:
                    log.error("ai_budget_hard_cap", user_ref=_user_ref(row.user_id), date=date)
    if cfg.global_daily_usd is not None:
        actions = []
        if total >= cfg.global_daily_usd * Decimal(str(cfg.global_alert_fraction)):
            actions.append(GLOBAL_ALERT_ACTION)
        if total >= cfg.global_daily_usd:
            actions.append(GLOBAL_REACHED_ACTION)
        async with uow_factory(user_id=None) as uow:
            for action in actions:
                if await _audited(uow, action, date, None):
                    continue
                await record_audit(uow, action, actor="system", metadata={"date": date})
                written += 1
                log.error("ai_global_budget", action=action, date=date)
    return written


@dataclass(frozen=True)
class CostRow:
    user_ref: str  # hashed user ID (BACKEND_DESIGN.md §19); "system" for calls without a user
    role: str
    calls: int
    est_cost_usd: Decimal


async def cost_by_user(uow: UnitOfWork, *, day: datetime.date) -> list[CostRow]:
    """Cost per user and role for one UTC day from the roll-ups (AI_COST_MODEL.md §8). Worker role:
    every user's rows; IDs are hashed, no content."""
    r = ai_cost_rollups_table
    start = datetime.datetime.combine(day, datetime.time(), tzinfo=datetime.UTC)
    rows = await uow.session.execute(
        select(
            r.c.user_id,
            r.c.role,
            func.sum(r.c.calls).label("calls"),
            func.sum(r.c.est_cost_usd).label("cost"),
        )
        .where(r.c.bucket_start >= start, r.c.bucket_start < start + datetime.timedelta(days=1))
        .group_by(r.c.user_id, r.c.role)
        .order_by(func.sum(r.c.est_cost_usd).desc())
    )
    return [
        CostRow(
            _user_ref(row.user_id) if row.user_id is not None else "system",
            row.role,
            int(row.calls),
            Decimal(str(row.cost)),
        )
        for row in rows
    ]
