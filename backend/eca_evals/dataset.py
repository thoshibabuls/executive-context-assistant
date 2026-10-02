"""Loading and validating the ``world_v1`` email slice (AI_EVALUATION.md §3.1).

Validation fails loudly on missing or inconsistent labels: every email has exactly one triage
label, every statement's evidence quote occurs verbatim in its email, every ``due_text`` occurs
in its evidence quote, and every label carries a ``label_status``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import yaml


class DatasetError(Exception):
    pass


@dataclass(frozen=True)
class EmailCase:
    case_id: str
    thread_id: str
    sender: str
    to: tuple[str, ...]
    subject: str
    body: str
    headers: dict[str, str]
    triage: dict[str, Any]
    statements: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class WorldV1:
    root: Path
    scenario: dict[str, Any]
    emails: tuple[EmailCase, ...]
    threads: dict[str, dict[str, Any]]

    def people_by_email(self) -> dict[str, str]:
        return {p["email"]: p["id"] for p in self.scenario["people"]}

    @property
    def user_id(self) -> str:
        return str(self.scenario["user"]["person"])


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise DatasetError(f"missing label file {path.name}")
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path.name}:{n}: invalid JSON") from exc
    return rows


def _parse_eml(path: Path) -> EmailMessage:
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    assert isinstance(msg, EmailMessage)
    return msg


def load_world_v1(root: Path) -> WorldV1:
    scenario = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    triage = _jsonl(root / "labels" / "triage.jsonl")
    items = _jsonl(root / "labels" / "items.jsonl")
    threads = {t["thread_id"]: t for t in _jsonl(root / "labels" / "threads.jsonl")}
    emails_dir = root / "sources" / "emails"
    files = sorted(emails_dir.glob("*.eml"))
    problems: list[str] = []

    triage_by_case: dict[str, dict[str, Any]] = {}
    for row in triage:
        if row.get("case_id") in triage_by_case:
            problems.append(f"duplicate triage label {row.get('case_id')}")
        triage_by_case[str(row.get("case_id"))] = row
    items_by_case: dict[str, list[dict[str, Any]]] = {}
    for row in items:
        items_by_case.setdefault(str(row.get("case_id")), []).append(row)

    cases: list[EmailCase] = []
    for path in files:
        case_id = path.stem
        msg = _parse_eml(path)
        body_part = msg.get_body(preferencelist=("plain",))
        body = body_part.get_content() if body_part is not None else ""
        label = triage_by_case.get(case_id)
        if label is None:
            problems.append(f"{case_id}: no triage label")
            continue
        for key in ("label_status", "needs_reply", "category", "thread_id"):
            if key not in label:
                problems.append(f"{case_id}: triage label lacks {key}")
        statements = tuple(items_by_case.get(case_id, []))
        for s in statements:
            if "label_status" not in s:
                problems.append(f"{s.get('statement_id')}: no label_status")
            if s.get("evidence_quote", "") not in body or not s.get("evidence_quote"):
                problems.append(f"{s.get('statement_id')}: evidence quote not found in the email")
            due = s.get("due_text")
            if due is not None and due not in s.get("evidence_quote", ""):
                problems.append(f"{s.get('statement_id')}: due_text not in the evidence quote")
        cases.append(
            EmailCase(
                case_id=case_id,
                thread_id=str(label.get("thread_id")),
                sender=str(msg["From"].addresses[0].addr_spec) if msg["From"] else "",
                to=tuple(a.addr_spec for a in msg["To"].addresses) if msg["To"] else (),
                subject=str(msg["Subject"] or ""),
                body=body,
                headers={k: str(v) for k, v in msg.items()},
                triage=label,
                statements=statements,
            )
        )
    known = {p.stem for p in files}
    problems += [f"{c}: label without email" for c in sorted(set(triage_by_case) - known)]
    problems += [f"{c}: statement without email" for c in sorted(set(items_by_case) - known)]
    problems += [f"{c.case_id}: unknown thread" for c in cases if c.thread_id not in threads]
    if problems:
        raise DatasetError("; ".join(problems[:20]))
    return WorldV1(root=root, scenario=scenario, emails=tuple(cases), threads=threads)
