"""Custom import-linter contract enforcing BACKEND_DESIGN.md §5.3.

Rules checked on every direct import inside ``eca``:

1. **Public APIs only.** A module may import another domain module only through its package
   root (``eca.<module>``), never its submodules. Composition modules (``eca.api``) may also
   import ``eca.<module>.router``. Shared modules (``eca.platform``) are importable everywhere.
2. **Layer order inside a domain module:** ``router``/``tasks`` → ``service`` → ``repository``
   → ``models``/``schemas``/``events``. A lower layer never imports a higher one, and routers
   and tasks do not import each other.
3. **No HTTP in core.** Shared modules and every domain submodule except ``router`` (including
   package roots) must not import HTTP framework packages.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from importlinter.application import output
from importlinter.domain import fields
from importlinter.domain.contract import Contract, ContractCheck

LAYER_RANK = {
    "router": 3,
    "tasks": 3,
    "service": 2,
    "repository": 1,
    "models": 0,
    "schemas": 0,
    "events": 0,
}
HTTP_ALLOWED_ROLES = {"router"}


@dataclass(frozen=True)
class Violation:
    rule: str
    importer: str
    imported: str

    def describe(self) -> str:
        return f"[{self.rule}] {self.importer} -> {self.imported}"


def _owner(module: str, candidates: Iterable[str]) -> str | None:
    for c in candidates:
        if module == c or module.startswith(c + "."):
            return c
    return None


def _role(module: str, owner: str) -> str | None:
    """First path component below the owning module, or None for the package root."""
    if module == owner:
        return None
    return module[len(owner) + 1 :].split(".", 1)[0]


def find_violations(
    imports: Iterable[tuple[str, str]],
    *,
    domain_modules: list[str],
    shared_modules: list[str],
    composition_modules: list[str],
    http_packages: list[str],
) -> list[Violation]:
    internal = [*domain_modules, *shared_modules, *composition_modules]
    violations: list[Violation] = []
    for importer, imported in imports:
        importer_owner = _owner(importer, internal)
        if importer_owner is None:
            continue
        imported_domain = _owner(imported, domain_modules)

        # Rule 1: cross-module imports go through the package root.
        if imported_domain is not None and imported_domain != importer_owner:
            submodule = _role(imported, imported_domain)
            allowed = submodule is None or (submodule == "router" and importer_owner in composition_modules)
            if not allowed:
                violations.append(Violation("public-api", importer, imported))

        # Rule 2: layer order inside one domain module.
        if importer_owner in domain_modules and imported_domain == importer_owner:
            src, dst = _role(importer, importer_owner), _role(imported, importer_owner)
            upward = (
                src in LAYER_RANK
                and dst in LAYER_RANK
                and (LAYER_RANK[dst] > LAYER_RANK[src] or (src != dst and {src, dst} == {"router", "tasks"}))
            )
            if upward:
                violations.append(Violation("layer-order", importer, imported))

        # Rule 3: no HTTP framework outside routers and composition modules.
        http = _owner(imported, http_packages)
        if http is not None and importer_owner not in composition_modules:
            role = _role(importer, importer_owner) if importer_owner in domain_modules else None
            if importer_owner in shared_modules or role not in HTTP_ALLOWED_ROLES:
                violations.append(Violation("no-http-in-core", importer, imported))
    return violations


class ModuleBoundaryContract(Contract):
    type_name = "eca_boundaries"

    domain_modules = fields.ListField(subfield=fields.StringField())
    shared_modules = fields.ListField(subfield=fields.StringField())
    composition_modules = fields.ListField(subfield=fields.StringField())
    http_packages = fields.ListField(subfield=fields.StringField())

    def check(self, graph: Any, verbose: bool) -> ContractCheck:
        pairs: list[tuple[str, str]] = []
        for importer in graph.modules:
            if not (importer == "eca" or importer.startswith("eca.")):
                continue
            for imported in graph.find_modules_directly_imported_by(importer):
                pairs.append((importer, imported))
        violations = find_violations(
            pairs,
            domain_modules=list(self.domain_modules),  # type: ignore[call-overload]
            shared_modules=list(self.shared_modules),  # type: ignore[call-overload]
            composition_modules=list(self.composition_modules),  # type: ignore[call-overload]
            http_packages=list(self.http_packages),  # type: ignore[call-overload]
        )
        return ContractCheck(kept=not violations, metadata={"violations": violations})

    def render_broken_contract(self, check: ContractCheck) -> None:
        for v in check.metadata["violations"]:
            output.print_error(v.describe(), bold=False)
            output.new_line()
