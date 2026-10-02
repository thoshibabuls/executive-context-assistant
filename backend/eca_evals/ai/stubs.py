"""Stub email pipelines: deterministic rules, no model (slice 0.5).

They exist so the runner, statistics and scorecard can be exercised end to end before the real
AI-01 pipeline exists (slice 1.4). They are not quality baselines for the product.

* ``RulesStubV1`` (baseline): simple regular expressions. It has a deliberate, realistic flaw:
  a promise inside forwarded content is attributed to the forwarder, which the safety gate must
  catch (``my_commitment`` for someone else's promise).
* ``RulesStubV2`` (candidate): v1 plus forwarded-content attribution, acceptances, third-party
  reports and vague dates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from eca_evals.dataset import EmailCase

USER = "p_user"
_DUE = re.compile(
    r"\b(by Friday|by October \d+|tomorrow|by end of next week|by Thursday|by the \d+(?:st|nd|rd|th)|soon)\b"
)
_SENTENCE = re.compile(r"[^.?!\n]*[.?!]")
_FORWARD_FROM = re.compile(r"^From: .*<([^>]+)>", re.MULTILINE)
_FORWARD_MARK = "---------- Forwarded message ---------"


@dataclass(frozen=True)
class PredStatement:
    statement_kind: str
    owner: str
    direction: str
    evidence_quote: str
    due_text: str | None


@dataclass(frozen=True)
class Prediction:
    needs_reply: bool
    category: str
    request_type: str
    prefilter_skip: bool
    statements: tuple[PredStatement, ...] = field(default_factory=tuple)


class RulesStubV1:
    name = "rules_stub_v1"
    vague_dates = False
    forwarded_attribution = False
    acceptances = False
    third_party_reports = False

    def __init__(self, people_by_email: dict[str, str]) -> None:
        self.people = people_by_email

    def person(self, email: str) -> str:
        return self.people.get(email, f"ext:{email}")

    def predict(self, case: EmailCase) -> Prediction:
        sender = self.person(case.sender)
        bulk = "List-Unsubscribe" in case.headers or "Auto-Submitted" in case.headers
        outbound = sender == USER
        body = case.body
        if bulk:
            category = "newsletter" if "List-Unsubscribe" in case.headers else "notification"
            return Prediction(False, category, "none", True)
        question = "?" in body
        if outbound:
            category, needs_reply = "other", False
        elif question and re.search(r"\bfree\b.*\bat\b", body):
            category, needs_reply = "scheduling", True
        elif question:
            category, needs_reply = "action", True
        else:
            category, needs_reply = "fyi", False
        request_type = "none"
        if needs_reply:
            lowered = body.lower()
            if category == "scheduling":
                request_type = "reply"
            elif "review" in lowered:
                request_type = "review"
            elif "approve" in lowered or "sign off" in lowered:
                request_type = "approve"
            elif "update" in lowered:
                request_type = "provide_info"
        return Prediction(needs_reply, category, request_type, False, tuple(self.statements(case, sender)))

    def _due(self, sentence: str) -> str | None:
        m = _DUE.search(sentence)
        if m is None or (m.group(1) == "soon" and not self.vague_dates):
            return None
        return m.group(1)

    def statements(self, case: EmailCase, sender: str) -> list[PredStatement]:
        body = case.body
        out: list[PredStatement] = []
        forwarded_owner: str | None = None
        cut = body.find(_FORWARD_MARK)
        if self.forwarded_attribution and cut >= 0:
            m = _FORWARD_FROM.search(body[cut:])
            forwarded_owner = self.person(m.group(1)) if m else None
        recipient = self.person(case.to[0]) if case.to else "unknown"
        for raw in _SENTENCE.findall(body):
            sentence = raw.strip()
            position = body.find(sentence)
            in_forward = cut >= 0 and position > cut
            promise = re.match(r"(I will|I'll|We'll|We will) ", sentence) is not None and any(
                verb in sentence for verb in ("send", "get the", "have it")
            )
            if promise:
                if in_forward and forwarded_owner:
                    out.append(
                        PredStatement("promise", forwarded_owner, "observed", sentence, self._due(sentence))
                    )
                elif sender == USER:
                    out.append(PredStatement("promise", USER, "my_commitment", sentence, self._due(sentence)))
                else:
                    out.append(PredStatement("promise", sender, "waiting_for", sentence, self._due(sentence)))
            elif re.match(r"(Could you|Can you) ", sentence) and sentence.endswith("?"):
                if sender == USER:
                    out.append(
                        PredStatement("request", recipient, "delegated", sentence, self._due(sentence))
                    )
                else:
                    out.append(PredStatement("request", USER, "my_task", sentence, self._due(sentence)))
            elif self.acceptances and sender == USER and sentence.startswith("Will do"):
                out.append(PredStatement("acceptance", USER, "my_commitment", sentence, self._due(sentence)))
            elif self.third_party_reports and re.match(r"[A-Z][a-z]+ will send ", sentence):
                name = sentence.split()[0]
                owner = next(
                    (pid for email, pid in self.people.items() if email.startswith(name.lower() + ".")),
                    "unknown",
                )
                out.append(
                    PredStatement("report_commitment", owner, "waiting_for", sentence, self._due(sentence))
                )
        return out


class RulesStubV2(RulesStubV1):
    name = "rules_stub_v2"
    vague_dates = True
    forwarded_attribution = True
    acceptances = True
    third_party_reports = True
