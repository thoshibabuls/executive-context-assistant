"""Slice 0.4: building the AI client from settings (BACKEND_DESIGN.md §5.5)."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from eca.intelligence import AIError, ConfigError, build_ai_client, load_ai_config
from eca.platform.config import Settings

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"
FAKE_KEY = "test-not-a-real-key-0000"


def _settings(**kw: Any) -> Settings:
    kw.setdefault("api_env", "test")
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


@pytest.fixture(scope="module")
def config() -> Any:
    return load_ai_config(REPO_CONFIG, today=datetime.date(2026, 10, 2))


def test_settings_defaults_are_live_without_key() -> None:
    s = _settings()
    assert (s.api_ai_mode, s.api_ai_config_dir, s.api_ai_cassette_dir, s.api_ai_timeout_s) == (
        "live",
        None,
        None,
        60.0,
    )


def test_record_mode_is_refused_in_production(config: Any, tmp_path: Path) -> None:
    s = _settings(
        api_env="production", api_ai_mode="record", api_ai_cassette_dir=str(tmp_path), gemini_api_key=FAKE_KEY
    )
    with pytest.raises(ConfigError, match="not allowed in production"):
        build_ai_client(s, uow_factory=None, config=config)


def test_replay_needs_a_cassette_dir_and_never_builds_a_provider(config: Any, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="API_AI_CASSETTE_DIR"):
        build_ai_client(_settings(api_ai_mode="replay"), uow_factory=None, config=config)
    client = build_ai_client(
        _settings(api_ai_mode="replay", api_ai_cassette_dir=str(tmp_path)), uow_factory=None, config=config
    )
    assert client.mode == "replay"
    assert client._provider is None  # replay cannot reach the network


def test_live_mode_needs_a_key_and_never_logs_it(config: Any) -> None:
    with pytest.raises(AIError, match="GEMINI_API_KEY is required"):
        build_ai_client(_settings(gemini_api_key=None), uow_factory=None, config=config)
    with capture_logs() as logs:
        client = build_ai_client(_settings(gemini_api_key=FAKE_KEY), uow_factory=None, config=config)
    assert client.mode == "live"
    assert FAKE_KEY not in repr(client.__dict__)
    assert FAKE_KEY not in repr(logs)
    assert FAKE_KEY not in repr(_settings(gemini_api_key=FAKE_KEY))


def test_config_dir_setting_is_used(tmp_path: Path) -> None:
    (tmp_path / "models.yaml").write_text("schema_version: 1\nroles: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        build_ai_client(_settings(api_ai_config_dir=str(tmp_path), gemini_api_key=FAKE_KEY), uow_factory=None)
