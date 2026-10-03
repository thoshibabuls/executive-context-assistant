"""Query planner: rules first, AI-05 only when no rule decides (AI_PIPELINE.md §4.2 O12, §5.8, §8.2).

The planner's output is a :class:`Plan` naming one code-defined retriever; it never writes SQL
(TECHNICAL_DESIGN.md §17.3). Rules are deterministic patterns over the question; names are
capitalized words that are not common sentence words (resolution against contacts happens in the
retriever, which asks when two persons match). AI-05 is skipped at the hard budget cap or when
no client is available; the question then goes to ``topic_status`` (planner ``fallback``).
"""

from __future__ import annotations

import re
from dataclasses import replace
from uuid import UUID

from eca import intelligence
from eca.intelligence import AIClient
from eca.retrieval.cards import delimit
from eca.retrieval.plan import Plan
from eca.retrieval.temporal import WEEKDAYS, extract_expression

_STOP = (
    frozenset(
        [
            "a",
            "an",
            "and",
            "any",
            "are",
            "as",
            "at",
            "be",
            "been",
            "but",
            "by",
            "can",
            "could",
            "did",
            "do",
            "does",
            "done",
            "for",
            "from",
            "get",
            "give",
            "got",
            "had",
            "has",
            "have",
            "he",
            "her",
            "hers",
            "him",
            "his",
            "how",
            "i",
            "i'd",
            "i'll",
            "i'm",
            "i've",
            "if",
            "in",
            "is",
            "it",
            "its",
            "just",
            "let",
            "list",
            "me",
            "my",
            "need",
            "needs",
            "next",
            "no",
            "not",
            "now",
            "of",
            "on",
            "or",
            "our",
            "out",
            "please",
            "she",
            "should",
            "show",
            "so",
            "tell",
            "than",
            "that",
            "the",
            "their",
            "them",
            "then",
            "there",
            "these",
            "they",
            "this",
            "those",
            "to",
            "today",
            "tomorrow",
            "up",
            "us",
            "was",
            "we",
            "were",
            "what",
            "whats",
            "what's",
            "when",
            "where",
            "which",
            "who",
            "who's",
            "whom",
            "whose",
            "why",
            "will",
            "with",
            "would",
            "yes",
            "yesterday",
            "you",
            "your",
            "about",
            "across",
            "after",
            "again",
            "all",
            "also",
            "any",
            "anything",
            "before",
            "both",
            "during",
            "each",
            "email",
            "emails",
            "meeting",
            "meetings",
            "project",
            "projects",
            "status",
            "update",
            "latest",
            "last",
            "week",
            "month",
            "day",
            "days",
            "new",
            "everyone",
            "someone",
        ]
    )
    | frozenset(WEEKDAYS)
    | frozenset(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
)
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_PRONOUN_PERSON = re.compile(r"\b(he|him|his|she|her|they|them)\b", re.IGNORECASE)
_PRONOUN_THING = re.compile(r"\b(it|that|this one|that one)\b", re.IGNORECASE)
_QUALIFIER = re.compile(r"\b(?:about|regarding|related to|on the|for the)\s+(.+?)[?.!]*$", re.IGNORECASE)
_TOPIC_LEADS = re.compile(
    r"^(?:what(?:'s| is) the (?:status|state|latest) (?:of|on|with)|what(?:'s| is) happening (?:with|on)|"
    r"where are we (?:on|with)|any (?:update|news|progress) on|(?:give me |)(?:an |the )?update on|"
    r"what did we decide (?:about|on)|what (?:has|have|did) .+? (?:said|say) about|"
    r"status of|how is|how are|tell me about)\s+",
    re.IGNORECASE,
)

# (pattern, intent, person_role) in priority order.
_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\bwho(?:'s| is| are)? (?:still )?waiting (?:on|for) me\b", re.I),
        "who_waiting_on_me",
        "any",
    ),
    (
        re.compile(
            r"\bwhat(?:'s| is| has)? (?:changed|new)\b|\bwhile i was (?:away|out|gone|off)\b|\bcatch me up\b",
            re.I,
        ),
        "what_changed",
        "any",
    ),
    (
        re.compile(r"\bwhat happened\b|\bwhat did i miss\b|\bhow was (?:yesterday|today)\b", re.I),
        "day_view",
        "any",
    ),
    (
        re.compile(
            r"\bwhat should i (?:do|work on|focus on|tackle)\b|\bwhat(?:'s| is) next\b|"
            r"\bnext (?:action|step)s?\b|\bfocus on today\b|\bwhere should i start\b",
            re.I,
        ),
        "next_action",
        "any",
    ),
    (
        re.compile(
            r"\b(?:need|needs|needing|awaiting) (?:my |a )?(?:response|reply|answer)\b|"
            r"\bhaven'?t (?:replied|answered)\b|\bwho should i (?:reply|respond|answer)\b|"
            r"\breply to first\b|\bunanswered\b",
            re.I,
        ),
        "needs_response",
        "any",
    ),
    (re.compile(r"\boverdue\b|\bpast due\b|\bmissed (?:deadline|due)", re.I), "overdue", "any"),
    (
        re.compile(r"\bdeadlines?\b|\bdue (?:today|tomorrow|this week|next week|soon)\b|\bcoming up\b", re.I),
        "deadlines",
        "any",
    ),
    (
        re.compile(
            r"\bwhat (?:did|have|has) i (?:promise|promised|commit|committed|agree|agreed)\b|"
            r"\bwhat do i owe\b|\bmy commitments\b|\bi promised\b",
            re.I,
        ),
        "promised",
        "counterparty",
    ),
    (re.compile(r"\bwaiting (?:for|on)\b|\b(?:owe|owes) me\b|\bowed to me\b", re.I), "waiting_for", "owner"),
    (
        re.compile(
            r"\bwhat (?:did|has|have) (?!i\b|we\b)[A-Z][\w'\-]*(?: [A-Z][\w'\-]*)? "
            r"(?:promise|promised|commit|committed|agree)\b"
        ),
        "waiting_for",
        "owner",
    ),
    (
        re.compile(r"\bwhat(?:'s| is) this (?:email |thread )?about\b|\bthis (?:email|thread)\b", re.I),
        "email_context",
        "any",
    ),
    (re.compile(r"\bproject\b", re.I), "project", "any"),
    (_TOPIC_LEADS, "topic_status", "any"),
    (
        re.compile(
            r"\bwhat did (?:i|we) (?:discuss|talk about) with\b|\bwhat(?:'s| is) going on with\b|\bwho is\b",
            re.I,
        ),
        "person",
        "any",
    ),
)


# Phase 4 meeting rules (AI_PIPELINE.md §5.10 "Meeting Q&A routing"): rules only; AI-05 unchanged.
_MEETING_PREP = re.compile(
    r"\bprep(?:are)? (?:me )?(?:for|before)\b|\bbrief me (?:for|before|on)\b|\bbefore (?:my|the|our) "
    r"(?:next )?(?:meeting|call|1:1|sync)\b|\bget ready for\b|\bwhat should i (?:ask|raise|bring up)\b",
    re.I,
)
_CROSS_MEETING = re.compile(
    r"\bacross (?:the |our |all )?(?:last |previous |past )?(?:\w+ )?meetings\b|\bover the (?:last|past) "
    r"(?:\w+ )?meetings\b|\bin (?:the |our )?(?:previous|earlier|past) meetings\b|"
    r"\bhow (?:has|have) .+? (?:evolved|changed) (?:across|over)\b",
    re.I,
)
_MEETING_LOOKUP = re.compile(
    r"\baction items?\b|\bnext steps\b|\bwhat was (?:decided|agreed)\b|\bwhat did we (?:decide|agree)\b|"
    r"\bdecisions?\b|\bopen questions?\b|\bwho (?:owns|is responsible|will|has to|took)\b|\bwho owes\b|"
    r"\bwhat do i (?:owe|need to do)\b|\bmy (?:tasks|action items)\b|\btakeaways?\b",
    re.I,
)
_MEETING_TRANSCRIPT = re.compile(
    r"\bwhat did \w[\w'\-]*(?: \w[\w'\-]*)? (?:say|mention|ask|suggest|propose)\b|"
    r"\bwho (?:said|mentioned|asked|suggested|raised)\b|"
    r"\bdid (?:anyone|anybody|someone|\w+) (?:say|mention|bring up|talk about)\b|"
    r"\bexact words\b|\bquote\b|\bwhen did .+? (?:say|mention)\b",
    re.I,
)
_MEETING_SYNTHESIS = re.compile(
    r"\bconcerns?\b|\brisks?\b|\bsummar(?:y|ise|ize)\b|\bmain (?:points|topics)\b|\bhow did (?:it|the "
    r"meeting) go\b|\bwhat (?:was|were) (?:discussed|the key)\b|\bwhat happened in\b|\btl;?dr\b|\bgist\b",
    re.I,
)


def plan_meeting(question: str, *, meeting_id: UUID | None) -> Plan | None:
    """Meeting intents by rules (Phase 4). In a meeting-scoped session the lookup, transcript and
    synthesis intents apply to that meeting; prep and cross-meeting questions apply anywhere."""
    expression, since = extract_expression(question)
    names = candidate_names(question)
    topic = _topic(question)
    if meeting_id is not None:
        for pattern, intent in (
            (_MEETING_TRANSCRIPT, "meeting_transcript"),
            (_MEETING_SYNTHESIS, "meeting_synthesis"),
            (_MEETING_LOOKUP, "meeting_lookup"),
        ):
            if pattern.search(question):
                return Plan(
                    intent=intent,
                    planner="rules",
                    person_names=tuple(names),
                    topic=topic,
                    time_expression=expression,
                    since=since,
                    meeting_id=meeting_id,
                )
    if _MEETING_PREP.search(question):
        return Plan(
            intent="meeting_prep",
            planner="rules",
            person_names=tuple(names),
            topic=topic,
            time_expression=expression,
            since=since,
            meeting_id=meeting_id,
        )
    if _CROSS_MEETING.search(question):
        return Plan(
            intent="cross_meeting",
            planner="rules",
            person_names=tuple(names),
            topic=topic,
            time_expression=expression,
            since=since,
            meeting_id=meeting_id,
        )
    return None


def candidate_names(question: str) -> list[str]:
    """Runs of capitalized words that are not common sentence words, as written (at most 3)."""
    names: list[str] = []
    run: list[str] = []
    for m in _WORD.finditer(question):
        word = m.group(0)
        if word[0].isupper() and word.lower().strip("'") not in _STOP and word.lower() != "i":
            run.append(word[:-2] if word.endswith("'s") else word)
            continue
        if run:
            names.append(" ".join(run))
            run = []
    if run:
        names.append(" ".join(run))
    return names[:3]


def _topic(question: str) -> str | None:
    text = question.strip().rstrip("?.! ")
    lead = _TOPIC_LEADS.match(text)
    if lead:
        text = text[lead.end() :]
    text = re.sub(r"^(?:the|our|my)\s+", "", text, flags=re.IGNORECASE).strip()
    return text[:120] or None


def plan_by_rules(question: str, *, conversation_id: UUID | None = None) -> Plan | None:
    """The rule plan, or None when no rule decides the intent (then AI-05 runs)."""
    expression, since = extract_expression(question)
    names = candidate_names(question)
    pronoun = (
        "person"
        if _PRONOUN_PERSON.search(question) and not names
        else ("thing" if _PRONOUN_THING.search(question) and not names else None)
    )
    for pattern, intent, role in _RULES:
        if not pattern.search(question):
            continue
        if intent == "email_context" and conversation_id is None:
            continue
        qualifier = None
        topic = None
        if intent in ("waiting_for", "promised", "needs_response", "overdue", "deadlines"):
            m = _QUALIFIER.search(question)
            if m:
                qualifier = m.group(1).strip()[:120]
        if intent in ("topic_status", "project"):
            topic = _topic(question)
        return Plan(
            intent=intent,
            planner="rules",
            # Topic and project questions carry their capitalized words in the topic, not as persons.
            person_names=() if intent in ("project", "topic_status") else tuple(names),
            person_role=role,
            pronoun=pronoun,
            topic=topic,
            time_expression=expression,
            since=since,
            conversation_id=conversation_id,
            qualifier=qualifier,
        )
    if names and len(question.split()) <= 6:
        return Plan(
            intent="person",
            planner="rules",
            person_names=tuple(names),
            time_expression=expression,
            since=since,
        )
    if pronoun == "person":
        return Plan(
            intent="person", planner="rules", pronoun="person", time_expression=expression, since=since
        )
    return None


async def plan_question(
    question: str,
    *,
    conversation_id: UUID | None,
    client: AIClient | None,
    allow_ai: bool,
    user_id: UUID | None,
    meeting_id: UUID | None = None,
) -> tuple[Plan, tuple[UUID, ...]]:
    """(plan, AI call IDs). Rules first; AI-05 only when they cannot decide (§8.2). Meeting
    intents (Phase 4) are decided by rules before the Phase 2 rules."""
    meeting_plan = plan_meeting(question, meeting_id=meeting_id)
    if meeting_plan is not None:
        return meeting_plan, ()
    ruled = plan_by_rules(question, conversation_id=conversation_id)
    if ruled is not None:
        return ruled, ()
    expression, since = extract_expression(question)
    fallback = Plan(
        intent="topic_status",
        planner="fallback",
        topic=_topic(question),
        time_expression=expression,
        since=since,
    )
    if client is None or not allow_ai:
        return fallback, ()
    call = await intelligence.run_plan_query(client, delimit(question), user_id=user_id)
    if call.output is None:
        return fallback, call.call_ids
    out = call.output
    time_expression: str | None = out.time_expression
    if time_expression == "since_date" and out.since_date:
        time_expression, since = out.since_date, True
    elif time_expression == "since_last_meeting":
        since = True  # resolved against meetings at assembly (Phase 4, CONTEXT_ARCHITECTURE.md §9.11)
    plan = Plan(
        intent=out.intent,
        planner="ai",
        person_names=tuple(n.strip()[:80] for n in out.person_names if n.strip()),
        topic=out.topic,
        time_expression=time_expression or expression,
        since=since or out.time_expression == "since_date",
        needs_clarification=out.needs_clarification,
        conversation_id=conversation_id,
        confidence=out.confidence,
    )
    if plan.intent == "email_context" and conversation_id is None:
        plan = replace(plan, intent="unsupported")
    return plan, call.call_ids
