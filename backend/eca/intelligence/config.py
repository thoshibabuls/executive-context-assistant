"""Loading the AI configuration and building the client (BACKEND_DESIGN.md §5.5).

``config/models.yaml``, ``config/pricing.yaml`` and ``config/budgets.yaml`` (repository root, or
``API_AI_CONFIG_DIR``) are loaded once at process start and validated: every model the registry
can call must have a price today. A failure is a startup error (``ConfigError``).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from eca.intelligence.budget import BudgetConfig, BudgetGuard
from eca.intelligence.provider.cassette import CassetteMode, CassetteStore
from eca.intelligence.provider.client import AIClient, Provider
from eca.intelligence.provider.meter import Meter
from eca.intelligence.provider.pricing import PriceTable
from eca.intelligence.provider.registry import RoleRegistry
from eca.intelligence.provider.types import AIError, ConfigError
from eca.platform.config import REPO_ROOT, Settings
from eca.platform.uow import UnitOfWorkFactory

DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class AIConfig:
    registry: RoleRegistry
    prices: PriceTable
    budgets: BudgetConfig


def load_ai_config(config_dir: Path | None = None, *, today: datetime.date | None = None) -> AIConfig:
    directory = config_dir or DEFAULT_CONFIG_DIR
    registry = RoleRegistry.from_file(directory / "models.yaml")
    prices = PriceTable.from_file(directory / "pricing.yaml")
    prices.check_covers(registry.model_ids(), today or datetime.datetime.now(datetime.UTC).date())
    budgets = BudgetConfig.from_file(directory / "budgets.yaml")
    return AIConfig(registry=registry, prices=prices, budgets=budgets)


def build_ai_client(
    settings: Settings,
    *,
    uow_factory: UnitOfWorkFactory | None,
    config: AIConfig | None = None,
    provider: Provider | None = None,
    global_budget: bool = False,
) -> AIClient:
    """The process's ``AIClient`` from settings (mode, key, cassette directory, timeout).

    ``record`` mode is refused in production; ``live`` and ``record`` need a key (or an injected
    provider). Replay mode never creates a provider, so it never reaches the network. With a
    database the client gets the budget guard (AI_COST_MODEL.md §7.2); ``global_budget`` is set by
    the worker, the only process that can read every user's spend.
    """
    mode: CassetteMode = settings.api_ai_mode
    if mode == "record" and settings.is_production:
        raise ConfigError("API_AI_MODE=record is not allowed in production")
    cfg = config or load_ai_config(Path(settings.api_ai_config_dir) if settings.api_ai_config_dir else None)
    cassettes = CassetteStore(Path(settings.api_ai_cassette_dir)) if settings.api_ai_cassette_dir else None
    if mode in ("replay", "record") and cassettes is None:
        raise ConfigError(f"API_AI_MODE={mode} needs API_AI_CASSETTE_DIR")
    if mode in ("live", "record") and provider is None:
        provider = _gemini(settings.gemini_api_key, settings.api_ai_timeout_s)
    return AIClient(
        registry=cfg.registry,
        prices=cfg.prices,
        mode=mode,
        provider=provider if mode != "replay" else None,
        cassettes=cassettes,
        meter=Meter(uow_factory) if uow_factory is not None else None,
        budget=(
            BudgetGuard(uow_factory, cfg.budgets, global_scope=global_budget)
            if uow_factory is not None
            else None
        ),
    )


def _gemini(api_key: SecretStr | None, timeout_s: float) -> Provider:
    if api_key is None or not api_key.get_secret_value():
        raise AIError("GEMINI_API_KEY is required for live or record mode")
    from eca.intelligence.provider.gemini import GeminiProvider

    return GeminiProvider(api_key, timeout_s=timeout_s)
