"""Deterministic safety checks around the language model.

The model is never the sole authority:
  * classify_input()  - keyword/regex screen of the transcript, independent of the model
  * validate_reply()  - post-checks every model reply and replaces unsafe/ungrounded text
Rules are intentionally conservative and are covered by tests/test_safety.py and the
scenario suite in eval/. They are a safety net, not a clinical safety guarantee.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schemas import SAFETY_ORDER, TOPIC_MIN_CONFIDENCE, AgentReply, LatencyMs, LLMOutput

# --------------------------------------------------------------------- input ---
_EMERGENCY = [
    r"\b(i('ve| have)?|i just) (fallen|fell)( down| over)?\b", r"\bfell (down|over)\b", r"\bon the floor\b",
    r"\bcan'?t get up\b", r"\bcannot get up\b",
    r"\b(can'?t|cannot|hard to|struggling to) breathe?\b", r"\bshort of breath\b", r"\bchoking\b",
    r"\bchest (pain|hurts|is tight)\b", r"\bheart attack\b", r"\b(having|had) a stroke\b",
    r"\b(there'?s a|on) fire\b", r"\bsmell (smoke|burning|gas)\b", r"\bsmoke alarm\b", r"\bsomething'?s burning\b",
    r"\b(house|kitchen|stove|room) is (burning|full of smoke)\b",
    r"\bbleeding\b", r"\b(lots of|so much) blood\b",
    r"\b(hurt|injured|cut|burned|burnt) (myself|my (head|hip|leg|arm|back|hand))\b",
    r"\b(terrible|bad|awful|a lot of|severe|so much) pain\b", r"\bin (a lot of )?pain\b", r"\bit hurts\b",
    r"\b(took|taken|swallowed) (too many|all (of )?(my|the)) (pills|tablets|medicine|medication)\b",
    r"\bcall (an )?ambulance\b", r"\bcall 911\b", r"\b(it'?s an|this is an|i have an) emergency\b",
    r"\bhelp me,? (please|i('m| am) (hurt|stuck|falling))\b", r"\bsomeone help\b",
    r"\b(feel|feeling) (very|really|so) (sick|ill|unwell|dizzy|faint)\b", r"\bpassing out\b", r"\bfainted\b",
]
_SELF_HARM = [
    r"\b(kill|hurt|harm) myself\b", r"\bend (my|it) (life|all)\b", r"\bwant to die\b", r"\bwish i (was|were) dead\b",
    r"\bdon'?t want to (live|be alive|be here anymore)\b", r"\bsuicid", r"\bno reason to live\b",
]
_LEAVING = [
    r"\b(want|need|have|going|got) to (go|get) (home|out|outside)\b", r"\b(go|going|get|take me) home\b",
    r"\bleave (the|this) (house|place|home)\b", r"\b(walk|go) out(side)?\b.*\b(night|dark|alone)\b",
    r"\bget (in|into) the car\b", r"\b(drive|driving) (to|home|somewhere|the car)\b", r"\bi'?ll drive\b",
    r"\bcatch (a|the) bus\b", r"\bgo to (work|the office)\b", r"\bpick up (the|my) (kids|children)\b",
]


def _any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return p
    return None


@dataclass
class InputScreen:
    flag: str = "none"           # none | notify_caregiver | emergency_guidance
    kind: str = "none"           # none | emergency | self_harm | leaving
    rule: str | None = None


def classify_input(transcript: str) -> InputScreen:
    t = transcript.lower()
    if (r := _any(_SELF_HARM, t)):
        return InputScreen("emergency_guidance", "self_harm", r)
    if (r := _any(_EMERGENCY, t)):
        return InputScreen("emergency_guidance", "emergency", r)
    if (r := _any(_LEAVING, t)):
        return InputScreen("notify_caregiver", "leaving", r)
    return InputScreen()


# -------------------------------------------------------------------- output ---
_BANNED: dict[str, str] = {
    "repetition_blame": r"\b(already (asked|told|said|mentioned)|as i (said|mentioned|told you)|i (just )?told you|you asked (me )?(that|this) before|you keep asking|again and again)\b",
    "infantilising": r"\b(sweetie|sweetheart|honey|hun|dearie|my dear|good (girl|boy)|little one|kiddo|buddy|darling)\b",
    "claims_human": r"\bi('m| am) (a |your )?(human|real person|person|daughter|son|wife|husband|mother|mom|mum|father|dad|nurse|doctor|therapist|carer|caregiver)\b",
    "claims_contact": r"\bi('ve| have| will|'ll)? ?(called|contacted|notified|messaged|texted|alerted|phoned|let .{1,20} know)\b",
    "claims_arrival": r"\b(is|are|'s|'re) (on (the|their|his|her) way|coming (soon|over|home|back|to see you|now|later|today|tonight))\b|\bwill be (here|home|back|there) (soon|shortly|in|at|later|today|tonight)\b",
    "emergency_impersonation": r"\bi('m| am) (an? )?(emergency|ambulance|911|paramedic)",
    "diagnosis": r"\b(you have|you've got|because of your|your) (dementia|alzheimer'?s?|memory loss|cognitive)\b",
    "memory_test": r"\bdo you remember (what|who|when|where|which|her name|his name)\b",
}

_NAME_ALLOW = {
    "i", "i'm", "i've", "i'll", "i'd", "ok", "okay", "god", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december", "christmas", "easter", "mom", "mum", "dad",
    "grandma", "grandpa", "english", "tv",
}

FALLBACK_REPLY = "I'm here with you. Would you like to tell me a little more?"
NOT_UNDERSTOOD_REPLY = "I'm sorry, I didn't catch that. Could you press the button and say it again?"


@dataclass
class ValidationReport:
    replaced: bool = False
    rules: list[str] = field(default_factory=list)
    trimmed_sentences: bool = False
    trimmed_questions: bool = False
    model_flag: str = "none"
    input_flag: str = "none"
    model_topic: str = "general"
    model_confidence: float = 0.0
    model_uses_context: bool = False


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def _ungrounded_names(text: str, allowed: set[str]) -> list[str]:
    bad = []
    for sentence in split_sentences(text):
        words = re.findall(r"[A-Za-z][A-Za-z'\-]*", sentence)
        for w in words[1:]:   # sentence-initial capitals are ordinary words
            if w[0].isupper() and w.lower() not in _NAME_ALLOW and w.lower().strip("'s") not in allowed:
                bad.append(w)
    return bad


def validate_reply(
    raw: LLMOutput | None,
    transcript: str,
    screen: InputScreen,
    approved_terms: set[str],
    approved_voice_ids: set[str],
    emergency_text: str,
    self_harm_text: str,
    prompt_version: str,
) -> tuple[AgentReply, ValidationReport]:
    rep = ValidationReport(input_flag=screen.flag)
    if raw is None:
        rep.replaced = True
        rep.rules.append("invalid_model_output")
        reply = AgentReply(reply_text=FALLBACK_REPLY, topic="general", topic_confidence=0.0,
                           uses_verified_context=False, safety_flag=screen.flag, prompt_version=prompt_version,
                           latency_ms=LatencyMs())
        if screen.flag == "emergency_guidance":
            reply.reply_text = self_harm_text if screen.kind == "self_harm" else emergency_text
        return reply, rep

    rep.model_flag, rep.model_topic = raw.safety_flag, raw.topic
    rep.model_confidence, rep.model_uses_context = raw.topic_confidence, raw.uses_verified_context

    # Safety flag: deterministic screen can only be escalated by the model, never lowered.
    flag = max(screen.flag, raw.safety_flag, key=lambda f: SAFETY_ORDER[f])
    text = re.sub(r"\s+", " ", raw.reply_text).strip()
    text = re.sub(r"[*_#`>\[\]{}]|•", "", text)   # no markdown / symbols for TTS

    if flag == "emergency_guidance":
        # Deterministic, pre-approved wording; the model's text is not used.
        text = self_harm_text if screen.kind == "self_harm" else emergency_text
        rep.replaced = True
        rep.rules.append("emergency_template")
        topic, conf, voice_id = "general", 0.0, None
    else:
        lower = text.lower()
        for rule, pat in _BANNED.items():
            if re.search(pat, lower):
                rep.rules.append(rule)
        allowed = approved_terms | {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", transcript)}
        bad_names = _ungrounded_names(text, allowed)
        if bad_names:
            rep.rules.append("ungrounded_name")
        if rep.rules:
            text = FALLBACK_REPLY
            rep.replaced = True
        sentences = split_sentences(text)
        q_idx = [i for i, s in enumerate(sentences) if s.endswith("?")]
        if len(q_idx) > 1:
            sentences = sentences[: q_idx[0] + 1]
            rep.trimmed_questions = True
        if len(sentences) > 3:
            sentences = sentences[:3]
            rep.trimmed_sentences = True
        text = " ".join(sentences)[:300].strip() or FALLBACK_REPLY
        topic = raw.topic
        conf = raw.topic_confidence
        if conf < TOPIC_MIN_CONFIDENCE:
            topic = "general"
        voice_id = raw.voice_message_id if raw.voice_message_id in approved_voice_ids and not rep.replaced else None
        if flag == "notify_caregiver" and topic in ("relaxation",):
            topic = "reassurance"

    lower = text.lower()
    uses_context = any(re.search(rf"\b{re.escape(t)}\b", lower) for t in approved_terms if len(t) > 2)
    reply = AgentReply(reply_text=text, topic=topic, topic_confidence=round(conf, 3),
                       uses_verified_context=uses_context, safety_flag=flag, prompt_version=prompt_version,
                       latency_ms=LatencyMs(), voice_message_id=voice_id)
    return reply, rep
