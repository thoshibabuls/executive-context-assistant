"""The custom import-linter contract detects each class of boundary violation."""

from __future__ import annotations

from eca_devtools.import_contracts import find_restricted_imports, find_violations

DOMAIN = ["eca.work", "eca.people", "eca.connectors", "eca.ingestion"]
OPTS = {
    "domain_modules": DOMAIN,
    "shared_modules": ["eca.platform"],
    "composition_modules": ["eca.api"],
    "http_packages": ["fastapi", "starlette"],
}


def rules(*pairs: tuple[str, str]) -> list[str]:
    return [v.rule for v in find_violations(pairs, **OPTS)]


def test_allowed_imports() -> None:
    assert (
        rules(
            ("eca.work.service", "eca.people"),  # other module's public API
            ("eca.work.service", "eca.work.repository"),  # downward layer
            ("eca.work.router", "eca.work.service"),
            ("eca.work.repository", "eca.platform.uow"),  # shared module
            ("eca.work.router", "fastapi"),  # HTTP in routers
            ("eca.api.app", "eca.work.router"),  # composition mounts routers
            ("eca.api.app", "fastapi"),
        )
        == []
    )


def test_private_submodule_of_another_module_is_rejected() -> None:
    assert rules(("eca.work.service", "eca.people.repository")) == ["public-api"]
    assert rules(("eca.work.service", "eca.people.models")) == ["public-api"]
    assert rules(("eca.work.router", "eca.people.router")) == ["public-api"]


def test_upward_layer_imports_are_rejected() -> None:
    assert rules(("eca.work.repository", "eca.work.service")) == ["layer-order"]
    assert rules(("eca.work.service", "eca.work.router")) == ["layer-order"]
    assert rules(("eca.work.models", "eca.work.repository")) == ["layer-order"]
    assert rules(("eca.work.tasks", "eca.work.router")) == ["layer-order"]


def test_http_outside_routers_is_rejected() -> None:
    assert rules(("eca.work.service", "fastapi")) == ["no-http-in-core"]
    assert rules(("eca.work", "starlette")) == ["no-http-in-core"]
    assert rules(("eca.platform.errors", "fastapi")) == ["no-http-in-core"]
    assert rules(("eca.work.tasks", "fastapi")) == ["no-http-in-core"]


def test_client_packages_use_public_roots_only() -> None:
    def client_rules(*pairs: tuple[str, str]) -> list[str]:
        return [v.rule for v in find_violations(pairs, **OPTS, client_modules=["eca_evals"])]

    assert client_rules(("eca_evals.ai.runner", "eca.work"), ("eca_evals.gate_a0", "eca.platform.ids")) == []
    assert client_rules(("eca_evals.ai.runner", "eca.work.service")) == ["public-api"]
    assert client_rules(("eca_evals.scorecard", "fastapi")) == ["no-http-in-core"]


def test_restricted_package_is_confined_to_its_owner() -> None:
    def restricted(*statements: tuple[str, str]) -> list[str]:
        found = find_restricted_imports(
            statements, package="google.genai", allowed_importers=["eca.intelligence"]
        )
        return [v.importer for v in found]

    assert (
        restricted(
            ("eca.intelligence.provider.gemini", "from google import genai"),
            ("eca.intelligence.provider.gemini", "from google.genai import errors as genai_errors"),
            ("eca.connectors.gmail", "from google.oauth2 import credentials"),  # other google packages
            ("eca.work.service", "import googleapis"),
        )
        == []
    )
    assert restricted(
        ("eca.work.service", "from google import genai"),
        ("eca.chat.service", "from google.genai import types"),
        ("eca.platform.x", "import google.genai"),
        ("eca_evals.ai.runner", "from google import auth, genai"),
    ) == ["eca.work.service", "eca.chat.service", "eca.platform.x", "eca_evals.ai.runner"]
