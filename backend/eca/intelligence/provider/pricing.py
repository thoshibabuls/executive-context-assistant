"""Prices with effective dates from ``config/pricing.yaml`` (AI_COST_MODEL.md §2) and call cost.

``effective_from`` is inclusive and ``effective_to`` exclusive (UTC dates). Arithmetic uses
``Decimal``. Cached input tokens are charged at the full input price (no cache discount is
assumed); thinking tokens are billed as output.
"""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from eca.intelligence.provider.types import ConfigError, PriceNotFound, Usage

MTOK = Decimal(1_000_000)
COST_QUANTUM = Decimal("0.00000001")  # ai_calls.est_cost_usd is numeric(14, 8)


class PriceEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effective_from: datetime.date
    effective_to: datetime.date | None = None
    input_per_mtok: Decimal | None = None
    audio_input_per_mtok: Decimal | None = None
    output_per_mtok: Decimal | None = None
    audio_input_per_minute: Decimal | None = None
    audio_output_per_minute: Decimal | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PriceEntry:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        prices = (
            self.input_per_mtok,
            self.audio_input_per_mtok,
            self.output_per_mtok,
            self.audio_input_per_minute,
            self.audio_output_per_minute,
        )
        if all(p is None for p in prices):
            raise ValueError("a price entry needs at least one price")
        if any(p is not None and p < 0 for p in prices):
            raise ValueError("prices must not be negative")
        return self

    def covers(self, day: datetime.date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day < self.effective_to)


class _PricingFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    currency: str
    source: str
    models: dict[str, list[PriceEntry]]

    @model_validator(mode="after")
    def _no_overlap(self) -> _PricingFile:
        if self.currency != "USD":
            raise ValueError("only USD prices are supported")
        for model, entries in self.models.items():
            ordered = sorted(entries, key=lambda e: e.effective_from)
            for a, b in itertools.pairwise(ordered):
                if a.effective_to is None or a.effective_to > b.effective_from:
                    raise ValueError(f"overlapping price entries for {model}")
        return self


class PriceTable:
    def __init__(self, models: dict[str, list[PriceEntry]]) -> None:
        self._models = models

    @classmethod
    def from_file(cls, path: Path) -> PriceTable:
        try:
            parsed = _PricingFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise ConfigError(f"Invalid pricing file {path.name}: {exc}") from exc
        return cls(parsed.models)

    def entry(self, model: str, day: datetime.date) -> PriceEntry:
        for candidate in self._models.get(model, []):
            if candidate.covers(day):
                return candidate
        raise PriceNotFound(f"No price for {model!r} on {day.isoformat()}")

    def cost(self, model: str, usage: Usage, *, day: datetime.date) -> Decimal:
        """Estimated USD cost of one call, quantized to 1e-8."""
        e = self.entry(model, day)
        total = Decimal(0)
        if e.audio_input_per_minute is not None or e.audio_output_per_minute is not None:
            minutes = Decimal(str(usage.audio_seconds)) / Decimal(60)
            total += minutes * ((e.audio_input_per_minute or 0) + (e.audio_output_per_minute or 0))
        text_input = usage.input_tokens - usage.audio_input_tokens
        audio_price = e.audio_input_per_mtok if e.audio_input_per_mtok is not None else e.input_per_mtok
        if e.input_per_mtok is not None:
            total += Decimal(max(text_input, 0)) * e.input_per_mtok / MTOK
        if audio_price is not None:
            total += Decimal(usage.audio_input_tokens) * audio_price / MTOK
        if e.output_per_mtok is not None:
            total += Decimal(usage.output_tokens + usage.thinking_tokens) * e.output_per_mtok / MTOK
        return total.quantize(COST_QUANTUM)

    def check_covers(self, models: set[str], day: datetime.date) -> None:
        """Startup check: every configured model has a price today."""
        missing = sorted(m for m in models if not any(e.covers(day) for e in self._models.get(m, [])))
        if missing:
            raise PriceNotFound(f"No price on {day.isoformat()} for: {', '.join(missing)}")
