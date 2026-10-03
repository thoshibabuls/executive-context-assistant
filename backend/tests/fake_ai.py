"""Deterministic in-process AI provider for tests (no network, no Gemini).

``FakeProvider`` answers every structured-output request with the smallest instance that its JSON
schema allows (empty arrays, empty strings, the first enum value, the minimum number), unless a
test scripts an answer for the schema's title. Embeddings are unit vectors derived from a hash of
each text, so equal texts get equal vectors and the result never depends on the process.

The client is built in ``record`` mode when a cassette directory is given, so a test can turn a
fake run into cassettes that a worker subprocess replays (``API_AI_MODE=replay``).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eca.intelligence import load_ai_config
from eca.intelligence.provider.cassette import CassetteStore
from eca.intelligence.provider.client import AIClient
from eca.intelligence.provider.meter import Meter
from eca.intelligence.provider.types import (
    EmbedRequest,
    EmbedResponse,
    FileRef,
    GenerateRequest,
    GenerateResponse,
    Usage,
)
from eca.platform.uow import UnitOfWorkFactory

Responder = Callable[[GenerateRequest], dict[str, Any] | str]


def minimal_instance(schema: dict[str, Any], root: dict[str, Any] | None = None) -> Any:
    """The smallest value valid for ``schema`` (enough for the schemas of ``eca.intelligence``)."""
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        target: Any = root
        for part in ref.removeprefix("#/").split("/"):
            target = target[part]
        return minimal_instance(target, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = schema[key]
            for option in options:
                if option.get("type") == "null":
                    return None
            return minimal_instance(options[0], root)
    if "allOf" in schema:
        return minimal_instance(schema["allOf"][0], root)
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = "null" if "null" in kind else kind[0]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {name: minimal_instance(props[name], root) for name in schema.get("required", [])}
    if kind == "array":
        return [minimal_instance(schema.get("items", {}), root) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        return "x" * int(schema.get("minLength", 0))
    if kind in ("integer", "number"):
        low = schema.get("minimum", schema.get("exclusiveMinimum", 0))
        return int(low) if kind == "integer" else float(low)
    if kind == "boolean":
        return False
    return None


def hash_vector(text: str, dims: int) -> tuple[float, ...]:
    raw: list[float] = []
    counter = 0
    while len(raw) < dims:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        raw.extend((b - 127.5) / 127.5 for b in digest)
        counter += 1
    vec = raw[:dims]
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return tuple(round(v / norm, 6) for v in vec)


class FakeProvider:
    def __init__(self, responders: dict[str, Responder] | None = None) -> None:
        # Keyed by the output schema's ``title`` (the pydantic model name).
        self.responders: dict[str, Responder] = dict(responders or {})
        self.generate_calls: list[GenerateRequest] = []
        self.embed_calls: list[EmbedRequest] = []
        self.uploads: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def generate(self, request: GenerateRequest) -> GenerateResponse:
        self.generate_calls.append(request)
        schema = request.response_json_schema or {}
        responder = self.responders.get(str(schema.get("title", "")))
        body: Any = responder(request) if responder is not None else minimal_instance(schema)
        text = body if isinstance(body, str) else json.dumps(body)
        return GenerateResponse(
            text=text,
            usage=Usage(input_tokens=100, output_tokens=20),
            model_version=request.model,
            finish_reason="STOP",
        )

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        self.embed_calls.append(request)
        dims = request.output_dimensionality or 768
        return EmbedResponse(
            vectors=tuple(hash_vector(t, dims) for t in request.texts),
            usage=Usage(input_tokens=sum(len(t) // 4 + 1 for t in request.texts)),
        )

    async def upload_file(self, path: Path, *, mime_type: str) -> FileRef:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: ASYNC240 (small test files)
        self.uploads.append((path.name, digest))
        return FileRef(name=f"files/{digest[:16]}", uri=f"fake://{digest}", mime_type=mime_type)

    async def delete_file(self, ref: FileRef) -> None:
        self.deleted.append(ref.name)

    def calls_for(self, title: str) -> list[GenerateRequest]:
        return [r for r in self.generate_calls if (r.response_json_schema or {}).get("title") == title]


def fake_ai_client(
    provider: FakeProvider | None = None,
    *,
    uow_factory: UnitOfWorkFactory | None = None,
    cassette_dir: Path | None = None,
) -> AIClient:
    """An ``AIClient`` over ``FakeProvider``: metered when ``uow_factory`` is given, recording
    cassettes when ``cassette_dir`` is given. No budget guard (budget tests build their own)."""
    config = load_ai_config()
    return AIClient(
        registry=config.registry,
        prices=config.prices,
        mode="record" if cassette_dir is not None else "live",
        provider=provider or FakeProvider(),
        cassettes=CassetteStore(cassette_dir) if cassette_dir is not None else None,
        meter=Meter(uow_factory) if uow_factory is not None else None,
    )
