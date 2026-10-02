"""The custom import-linter contract detects each class of boundary violation."""

from __future__ import annotations

from eca_devtools.import_contracts import find_violations

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
