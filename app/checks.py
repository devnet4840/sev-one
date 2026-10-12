"""Deterministic checks on every AI answer, run before the engineer sees anything.

These are the main blocker. An AI judge's score can vary from run to run, so the decision that
matters most in the demo (blocking an unsafe recommendation) must not depend on it alone.

The checks are deliberately strict: when unsure, they block, and the engineer gets the standard
runbook message built from the same incident data. A false block costs a nicer explanation;
a missed block can cost an outage made worse at 2 a.m.

1. Every command in the answer is an exact runbook command.
2. Every version, percentage, millisecond value, and clock time matches the incident data.
3. No recommendation of an action the policy marks "allowed": false (database failover, cache
   flush, silencing alerts), unless the answer says it needs the incident commander's approval.
4. No claim that the assistant ran, restarted, or rolled back anything (its tools only read).
5. The answer is not empty.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from . import data


@dataclass
class CheckResult:
    name: str
    label: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_NEGATION = re.compile(
    r"\b(not|no|never|isn't|aren't|wasn't|weren't|won't|can't|cannot|don't|doesn't|didn't|unable|excluded|outside|without)\b",
    re.IGNORECASE,
)
# Reassurance phrases contain negation words but do not negate what follows.
_REASSURANCE = re.compile(
    r"\b(?:don't|do not|no need to|not to) (?:worry|stress|fret)\b|\bno (?:worries|problem|stress)\b|\bnever fear\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# Contrast words start a new thought: "Don't flush the cache, but fail over the database now."
# A recommendation and a "not" on opposite sides of one of these never affect each other.
_CONTRAST_SPLIT = re.compile(r"\b(?:but|however|though|although|whereas|yet)\b", re.IGNORECASE)
# "While a failover isn't needed, flush the cache": the comma ends the "while" part.
_LEADING_CONCESSION = re.compile(r"^\s*(?:while|although|though|even though|whereas)\b[^,]*,", re.IGNORECASE)
_CLAUSE_BOUNDARY = re.compile(r"[,;:]")


def _segments(sentence: str) -> list[str]:
    out = []
    for part in _CONTRAST_SPLIT.split(sentence):
        m = _LEADING_CONCESSION.match(part)
        if m:
            out.extend([part[: m.end()], part[m.end() :]])
        else:
            out.append(part)
    return [seg for seg in out if seg.strip()]


def _clause_start(text: str, index: int) -> int:
    """Start of the comma/colon/semicolon clause that contains position ``index``."""
    starts = [m.end() for m in _CLAUSE_BOUNDARY.finditer(text, 0, index)]
    return starts[-1] if starts else 0


def _negated(text: str, match_start: int, matched: str = "", window_words: int = 5) -> bool:
    """True if a "not" applies to the match: inside it, or in the few words before it within the
    same clause. Reassurances like "don't worry" are ignored."""
    if _NEGATION.search(_REASSURANCE.sub(" ", matched)):
        return True
    before = _REASSURANCE.sub(" ", text[_clause_start(text, match_start) : match_start])
    return bool(_NEGATION.search(" ".join(before.split()[-window_words:])))


# ---------------------------------------------------------------------------
# 1. Commands
# ---------------------------------------------------------------------------

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_COMMAND_RE = re.compile(r"\b(?:kubectl|dbctl|cachectl|helm|redis-cli|psql|systemctl|docker)\b[^\n`]*", re.IGNORECASE)
_TRAILING = " .,;:!?)\"'"


def _norm(cmd: str) -> str:
    return " ".join(cmd.split()).strip(_TRAILING)


def _known_command(found: str, allowed: set[str]) -> bool:
    found = _norm(found)
    for c in allowed:
        if found == c:
            return True
        if found.startswith(c + " "):
            # Prose may continue after a command on the same line ("... -n prod and watch the
            # error rate"), but extra flags, pipes, or redirects make it a different command.
            first = found[len(c) :].split()[0]
            if not first.startswith(("-", "|", ">", "<", "&", ";")) and "=" not in first and "/" not in first:
                return True
    return False


def check_commands(text: str, allowed: set[str]) -> CheckResult:
    allowed = {_norm(c) for c in allowed}
    found = [m.group(1) for m in _BACKTICK_RE.finditer(text)]
    outside = _BACKTICK_RE.sub(" ", text)
    found += [m.group(0) for m in _COMMAND_RE.finditer(outside)]
    unknown = sorted({_norm(f) for f in found if not _known_command(f, allowed)})
    return CheckResult(
        name="grounded_commands",
        label="Commands are exact runbook commands",
        passed=not unknown,
        detail=f"Not in the runbook: {', '.join(unknown)}" if unknown else f"{len(found)} command(s) verified",
    )


# ---------------------------------------------------------------------------
# 2. Versions, numbers, and times
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"\bv\d+\.\d+\.\d+\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent\b|ms\b|milliseconds?\b|replicas?\b)", re.IGNORECASE)


def check_versions(text: str, allowed: set[str]) -> CheckResult:
    found = {m.group(0).lower() for m in _VERSION_RE.finditer(text)}
    unknown = sorted(found - {v.lower() for v in allowed})
    return CheckResult(
        name="grounded_versions",
        label="Versions match the incident data",
        passed=not unknown,
        detail=f"Not in the incident data: {', '.join(unknown)}" if unknown else f"{len(found)} version(s) verified",
    )


def check_numbers(text: str, allowed: set[float]) -> CheckResult:
    found = [(m.group(0), float(m.group(1).replace(",", ""))) for m in _NUMBER_RE.finditer(text)]
    unknown = sorted({raw for raw, value in found if value not in allowed})
    return CheckResult(
        name="grounded_numbers",
        label="Metrics match the incident data",
        passed=not unknown,
        detail=f"Not in the incident data: {', '.join(unknown)}" if unknown else f"{len(found)} number(s) verified",
    )


_TIME_12H_RE = re.compile(
    r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?\s?m\.?(?![a-z])",
    re.IGNORECASE,
)
_TIME_24H_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")


def _to_minutes_12h(hour: int, minute: int, half: str) -> int:
    hour = hour % 12
    if half.lower() == "p":
        hour += 12
    return hour * 60 + minute


def check_times(text: str, allowed_minutes: set[int]) -> CheckResult:
    found: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for m in _TIME_12H_RE.finditer(text):
        minutes = _to_minutes_12h(int(m.group(1)), int(m.group(2) or 0), m.group(3))
        found.append((m.group(0).strip(), minutes))
        spans.append(m.span())
    ambiguous: list[tuple[str, set[int]]] = []
    for m in _TIME_24H_RE.finditer(text):
        if any(start <= m.start() < end for start, end in spans):
            continue  # already counted as a 12-hour time
        hour, minute = int(m.group(1)), int(m.group(2))
        readings = {hour * 60 + minute}
        if 1 <= hour <= 12:
            # "8:40 tonight" has no AM/PM. Accept it if either reading is a real time.
            readings.add(((hour % 12) + 12) * 60 + minute)
            readings.add((hour % 12) * 60 + minute)
        ambiguous.append((m.group(0), readings))
    unknown = sorted(
        {raw for raw, minutes in found if minutes not in allowed_minutes}
        | {raw for raw, readings in ambiguous if not readings & allowed_minutes}
    )
    found_count = len(found) + len(ambiguous)
    return CheckResult(
        name="grounded_times",
        label="Times match the incident data",
        passed=not unknown,
        detail=f"Not in tool results: {', '.join(unknown)}" if unknown else f"{found_count} time(s) verified",
    )


# ---------------------------------------------------------------------------
# 3. Recommending actions that need the incident commander
# ---------------------------------------------------------------------------

# Words that identify each action. Keys match data.POLICY["items"].
_ACTION_WORDS: dict[str, list[str]] = {
    "database_failover": [r"fail(?:ing|s|ed)?\s?-?\s?over", r"failovers?", r"promote (?:the )?replica", r"switch (?:the )?primary", r"dbctl failover"],
    "flush_cache": [r"flush(?:ing|es|ed)?", r"clear(?:ing)? (?:the |out the )?(?:shared |query )?cache", r"wip(?:e|ing) (?:the )?cache", r"cachectl flush"],
    "silence_alerts": [r"silenc(?:e|ing)", r"snooz(?:e|ing)", r"mut(?:e|ing) (?:the |this |that )?alerts?", r"disabl(?:e|ing) (?:the |this |that )?alerts?", r"turn(?:ing)? off (?:the |this |that )?alerts?"],
}

# The answer may mention a gated action if it says a person has to approve it.
_APPROVAL = re.compile(
    r"\b(?:approv\w*|incident commander|\bIC\b|sign-?off|escalat\w*)", re.IGNORECASE
)
# "A failover is not an option" puts the "not" after the action, so look for these too.
_AFTER_NEGATION = re.compile(
    r"\b(?:is|are|would be)\s+not\b|\bisn't\b|\baren't\b|\bnot (?:allowed|recommended|permitted|safe|an option|needed|necessary)\b"
    r"|\b(?:hold off|avoid|wait)\b",
    re.IGNORECASE,
)


def _action_in(text: str, action: str) -> re.Match | None:
    for word in _ACTION_WORDS.get(action, []):
        m = re.search(rf"\b{word}\b", text, re.IGNORECASE)
        if m:
            return m
    return None


def _recommends(segment: str, action: str) -> bool:
    m = _action_in(segment, action)
    if not m:
        return False
    if _negated(segment, m.start(), m.group(0)):
        return False
    if _APPROVAL.search(segment) or _AFTER_NEGATION.search(segment):
        return False
    return True


def check_unapproved_actions(text: str, policy: dict) -> CheckResult:
    gated = [key for key, item in policy["items"].items() if not item["allowed"]]
    problems: list[str] = []
    for sentence in filter(None, (s.strip() for s in _SENTENCE_SPLIT.split(text))):
        segments = _segments(sentence)
        for action in gated:
            if any(_recommends(seg, action) for seg in segments):
                problems.append(f'{action.replace("_", " ")} (needs approval): "{sentence}"')
    return CheckResult(
        name="no_unapproved_actions",
        label="No recommending actions that need approval",
        passed=not problems,
        detail=" | ".join(problems) if problems else "No unapproved action recommended",
    )


# ---------------------------------------------------------------------------
# 4. Claims of having done something (the tools only read)
# ---------------------------------------------------------------------------

_DONE = (
    r"(?:rolled back|restarted|rebooted|scaled(?: up| out)?|ran|run|executed|flushed|failed over|reverted"
    r"|silenced|snoozed|muted|deployed|redeployed|applied|triggered|kicked off|initiated|fixed|resolved|mitigated)"
)
_ACTION_CLAIMS = [
    re.compile(rf"\b(?:i|we)(?:'ve| have| had)?\s+(?:already\s+|just\s+|now\s+|gone ahead and\s+|went ahead and\s+)?{_DONE}\b", re.IGNORECASE),
    re.compile(rf"\b(?:has|have)\s+(?:already\s+|now\s+)?been\s+{_DONE}\b", re.IGNORECASE),
    re.compile(r"\b(?:i|we)(?:'ll| will| am going to|'m going to)\s+(?:now\s+|go ahead and\s+)?(?:roll back|restart|scale|run|execute|flush|fail over|revert|silence|apply|deploy)\b", re.IGNORECASE),
]


def check_no_action_claims(text: str) -> CheckResult:
    hits = []
    for pattern in _ACTION_CLAIMS:
        for m in pattern.finditer(text):
            # "I have not restarted anything" is not a claim.
            if _negated(text, m.start(), m.group(0), window_words=3):
                continue
            hits.append(m.group(0))
    return CheckResult(
        name="no_action_claims",
        label="No claim that the assistant ran anything",
        passed=not hits,
        detail=f"Claims an action its tools cannot take: {', '.join(hits)}" if hits else "No action claims",
    )


# ---------------------------------------------------------------------------
# 5. Non-empty
# ---------------------------------------------------------------------------


def check_not_empty(text: str) -> CheckResult:
    ok = bool(text and text.strip())
    return CheckResult(
        name="answer_present",
        label="Answer is not empty",
        passed=ok,
        detail="" if ok else "The model returned no text",
    )


def run_checks(text: str, policy: dict | None = None) -> list[CheckResult]:
    policy = policy or data.POLICY
    text = text or ""
    return [
        check_not_empty(text),
        check_commands(text, data.known_commands()),
        check_versions(text, data.known_versions()),
        check_numbers(text, data.known_numbers()),
        check_times(text, data.known_times_in_minutes()),
        check_unapproved_actions(text, policy),
        check_no_action_claims(text),
    ]


def all_passed(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results)
