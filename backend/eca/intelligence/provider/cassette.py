"""Cassettes: recorded model responses keyed by (role, prompt_version, input_hash).

BACKEND_DESIGN.md §5.5. ``input_hash`` is the SHA-256 of the canonical JSON of the whole
request (model ID, generation settings, system instruction, contents, output schema), so any
prompt, model or setting change is a miss. Replay never reaches the network: a miss raises
``CassetteMiss``. One JSON file per entry: ``<root>/<role>/<prompt_version>/<input_hash>.json``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from eca.intelligence.provider.types import (
    CassetteMiss,
    EmbedRequest,
    EmbedResponse,
    FileRef,
    GenerateRequest,
    GenerateResponse,
    Usage,
)

CassetteMode = Literal["live", "replay", "record"]
FORMAT_VERSION = 1
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _canonical(value: Any) -> Any:
    if isinstance(value, FileRef):
        # Files are identified by content elsewhere; the key uses MIME type and URI only.
        return {"file_uri": value.uri, "mime_type": value.mime_type}
    if isinstance(value, tuple | list):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    return value


def input_hash(request: GenerateRequest | EmbedRequest) -> str:
    """SHA-256 over the canonical JSON of the full request."""
    body = {
        "kind": type(request).__name__,
        **{f.name: getattr(request, f.name) for f in dataclasses.fields(request)},
    }
    blob = json.dumps(_canonical(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CassetteKey:
    role: str
    prompt_version: str
    input_hash: str

    def __post_init__(self) -> None:
        for segment in (self.role, self.prompt_version):
            if not _SAFE_SEGMENT.fullmatch(segment):
                raise ValueError(f"unsafe cassette key segment {segment!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.input_hash):
            raise ValueError("input_hash must be a SHA-256 hex digest")

    def relative_path(self) -> Path:
        return Path(self.role) / self.prompt_version / f"{self.input_hash}.json"


def _usage_dict(usage: Usage) -> dict[str, Any]:
    return dataclasses.asdict(usage)


class CassetteStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: CassetteKey) -> Path:
        return self.root / key.relative_path()

    def get_generate(self, key: CassetteKey) -> GenerateResponse:
        data = self._load(key, "generate")
        return GenerateResponse(
            text=data["response"]["text"],
            usage=Usage(**data["response"]["usage"]),
            model_version=data["response"].get("model_version"),
            finish_reason=data["response"].get("finish_reason"),
        )

    def get_embed(self, key: CassetteKey) -> EmbedResponse:
        data = self._load(key, "embed")
        vectors = tuple(tuple(float(x) for x in v) for v in data["response"]["vectors"])
        return EmbedResponse(vectors=vectors, usage=Usage(**data["response"]["usage"]))

    def put_generate(self, key: CassetteKey, model: str, response: GenerateResponse) -> Path:
        return self._write(
            key,
            model,
            "generate",
            {
                "text": response.text,
                "usage": _usage_dict(response.usage),
                "model_version": response.model_version,
                "finish_reason": response.finish_reason,
            },
        )

    def put_embed(self, key: CassetteKey, model: str, response: EmbedResponse) -> Path:
        return self._write(
            key,
            model,
            "embed",
            {"vectors": [list(v) for v in response.vectors], "usage": _usage_dict(response.usage)},
        )

    def _load(self, key: CassetteKey, kind: str) -> dict[str, Any]:
        path = self._path(key)
        if not path.is_file():
            raise CassetteMiss(f"No cassette for {key.role}/{key.prompt_version}/{key.input_hash[:12]}")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format_version") != FORMAT_VERSION or data.get("kind") != kind:
            raise CassetteMiss(f"Cassette {path.name} has an unexpected format or kind")
        if (data.get("role"), data.get("prompt_version"), data.get("input_hash")) != (
            key.role,
            key.prompt_version,
            key.input_hash,
        ):
            raise CassetteMiss(f"Cassette {path.name} does not match its key")
        return data

    def _write(self, key: CassetteKey, model: str, kind: str, response: dict[str, Any]) -> Path:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": FORMAT_VERSION,
            "kind": kind,
            "role": key.role,
            "prompt_version": key.prompt_version,
            "input_hash": key.input_hash,
            "model": model,
            "response": response,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
