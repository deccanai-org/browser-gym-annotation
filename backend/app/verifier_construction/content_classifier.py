"""Small-model yes/no classifier for message-content checkpoints.

Used at **score time** (and Orchestrator golden/initial gates) for
``message_content_classifier`` predicates. Concept + message source path are
fixed once at Discriminator suite-generation time; evaluation is a cheap
classification call, not a token/string dict lookup.

Default model: ``claude-haiku-4-5-20251001`` (same cheap Haiku tier the
Discriminator already prefers for decomposition).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# Cheap classification model — keep in lockstep with Discriminator default.
DEFAULT_CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

_API = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Approximate Haiku 4.5 list prices (USD / 1M tokens) for cost reporting.
_HAIKU_INPUT_PER_MTOK = 1.0
_HAIKU_OUTPUT_PER_MTOK = 5.0

ClassifyFn = Callable[[str, str], bool]

# Test / offline injection. When set, skips the live API.
_classify_fn: ClassifyFn | None = None

# Last live call usage (for marginal-cost reporting).
_last_usage: dict[str, Any] = {
    "input_tokens": 0,
    "output_tokens": 0,
    "model": DEFAULT_CLASSIFIER_MODEL,
    "approx_cost_usd": 0.0,
    "calls": 0,
}


def set_content_classifier_fn(fn: ClassifyFn | None) -> None:
    """Inject a sync classifier for offline tests (``(concept, message) -> bool``)."""
    global _classify_fn
    _classify_fn = fn


def reset_classifier_usage() -> None:
    global _last_usage
    _last_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "model": DEFAULT_CLASSIFIER_MODEL,
        "approx_cost_usd": 0.0,
        "calls": 0,
    }


def get_classifier_usage() -> dict[str, Any]:
    return dict(_last_usage)


def _anthropic_api_key() -> str:
    try:
        from app.config import settings

        key = (settings.anthropic_api_key or "").strip()
        if key:
            return key
    except Exception:
        pass
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _approx_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000.0 * _HAIKU_INPUT_PER_MTOK
        + output_tokens / 1_000_000.0 * _HAIKU_OUTPUT_PER_MTOK
    )


def _call_classifier_api(prompt: str, *, model: str) -> tuple[str | None, dict[str, int]]:
    key = _anthropic_api_key()
    if not key:
        return None, {"input_tokens": 0, "output_tokens": 0}
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        _API,
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None, {"input_tokens": 0, "output_tokens": 0}
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage") or {}
    return text, {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


_YES_RE = re.compile(r"^\s*yes\b", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*no\b", re.IGNORECASE)


def classify_message_conveys(
    concept: str,
    message_text: str,
    *,
    model: str | None = None,
) -> bool:
    """Return True iff ``message_text`` conveys ``concept`` (paraphrase OK)."""
    concept = (concept or "").strip()
    message_text = (message_text or "").strip()
    if not concept or not message_text:
        return False

    if _classify_fn is not None:
        return bool(_classify_fn(concept, message_text))

    model_id = model or DEFAULT_CLASSIFIER_MODEL
    prompt = (
        "You are a strict yes/no classifier for ecommerce agent evaluation.\n"
        "Decide whether the MESSAGE conveys the CONCEPT (same meaning is enough; "
        "exact wording is NOT required). Negations and refusals count when the "
        "concept is a disclosure/decline.\n\n"
        f"CONCEPT:\n{concept}\n\n"
        f"MESSAGE:\n{message_text}\n\n"
        "Answer with YES or NO only."
    )
    text, usage = _call_classifier_api(prompt, model=model_id)
    global _last_usage
    _last_usage["calls"] = int(_last_usage.get("calls") or 0) + 1
    _last_usage["model"] = model_id
    _last_usage["input_tokens"] = int(_last_usage.get("input_tokens") or 0) + usage[
        "input_tokens"
    ]
    _last_usage["output_tokens"] = int(_last_usage.get("output_tokens") or 0) + usage[
        "output_tokens"
    ]
    _last_usage["approx_cost_usd"] = _approx_cost(
        int(_last_usage["input_tokens"]), int(_last_usage["output_tokens"])
    )
    if text is None:
        # Fail closed when the classifier is unavailable.
        return False
    stripped = text.strip()
    if _YES_RE.match(stripped):
        return True
    if _NO_RE.match(stripped):
        return False
    # Ambiguous → fail closed.
    return False


def _iter_sent_messages(state: dict[str, Any]) -> list[dict[str, Any]]:
    mail = state.get("mail") if isinstance(state.get("mail"), dict) else {}
    sent = mail.get("sent") or {}
    if isinstance(sent, dict):
        return [m for m in sent.values() if isinstance(m, dict)]
    if isinstance(sent, list):
        return [m for m in sent if isinstance(m, dict)]
    return []


def extract_classifier_messages(
    predicate: dict[str, Any], state: dict[str, Any]
) -> list[str]:
    """Pull relevant message blobs for a classifier predicate.

    ``source`` defaults to ``mail.sent``. Optional ``to`` filters recipients.
    """
    source = str(predicate.get("source") or "mail.sent").strip().lower()
    want_to = str(predicate.get("to") or "").lower().strip() or None
    blobs: list[str] = []

    if source in {"mail.sent", "mail", "sent"}:
        for msg in _iter_sent_messages(state):
            to = str(msg.get("to") or "")
            if want_to and want_to not in to.lower():
                continue
            blob = f"{msg.get('subject', '')}\n{msg.get('body', '')}".strip()
            if blob:
                blobs.append(blob)
        # If recipient filter emptied the list, fall back to all sent (same as
        # token-match helper) so a wrong `to` does not silently skip content.
        if not blobs and want_to:
            for msg in _iter_sent_messages(state):
                blob = f"{msg.get('subject', '')}\n{msg.get('body', '')}".strip()
                if blob:
                    blobs.append(blob)
        return blobs

    # Generic dotted path to a string field (rare).
    cur: Any = state
    for part in source.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return []
        cur = cur[part]
    if isinstance(cur, str) and cur.strip():
        return [cur.strip()]
    return []


def eval_message_content_classifier(
    predicate: dict[str, Any], state: dict[str, Any]
) -> bool:
    """True when any relevant message conveys the fixed ``concept``."""
    concept = str(predicate.get("concept") or "").strip()
    if not concept:
        return False
    messages = extract_classifier_messages(predicate, state)
    if not messages:
        return False
    model = predicate.get("model")
    model_s = str(model) if model else None
    for msg in messages:
        if classify_message_conveys(concept, msg, model=model_s):
            return True
    return False


def coerce_token_content_to_classifier(
    predicate: dict[str, Any],
    *,
    assertion: str = "",
    subgoal: str = "",
) -> dict[str, Any]:
    """Rewrite legacy token content predicates into classifier definitions.

    Classifier definition wins: tokens are dropped; concept is taken from
    assertion / subgoal (or a token-derived fallback).
    """
    if not isinstance(predicate, dict):
        return predicate
    kind = str(predicate.get("kind") or "")
    if kind == "message_content_classifier":
        out = dict(predicate)
        out.setdefault("source", "mail.sent")
        if not str(out.get("concept") or "").strip():
            out["concept"] = (assertion or subgoal or "required message content").strip()
        return out
    if kind != "mail_sent_contains_any":
        return predicate
    concept = (assertion or subgoal or "").strip()
    # Prefer a meaning-bearing phrase; bare "disclosure tokens" is not a concept.
    weak = (
        not concept
        or concept.lower() in {"disclosure tokens", "tokens", "mail", "email", "content"}
        or (concept.lower().startswith("disclosure") and len(concept.split()) <= 2)
    )
    if weak:
        tokens = [str(t).strip() for t in (predicate.get("tokens") or []) if str(t).strip()]
        if tokens:
            concept = (
                "the message truthfully discloses that: " + "; ".join(tokens[:10])
            )
        elif subgoal.strip():
            concept = subgoal.strip()
        else:
            concept = "the required truthful disclosure content"
    out: dict[str, Any] = {
        "kind": "message_content_classifier",
        "concept": concept,
        "source": "mail.sent",
    }
    if predicate.get("to"):
        out["to"] = predicate["to"]
    return out


__all__ = [
    "DEFAULT_CLASSIFIER_MODEL",
    "ClassifyFn",
    "classify_message_conveys",
    "coerce_token_content_to_classifier",
    "eval_message_content_classifier",
    "extract_classifier_messages",
    "get_classifier_usage",
    "reset_classifier_usage",
    "set_content_classifier_fn",
]
