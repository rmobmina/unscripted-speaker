"""Conversation orchestration: prompt assembly -> LLM -> validation.

Transport, storage and audio live elsewhere; this module is pure orchestration
and is reused unchanged by the live pipeline, the dashboard text tester and the
scenario evaluation suite.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .context import ApprovedContext
from .providers import LLMProvider, LLMResult, ProviderError
from .safety import InputScreen, ValidationReport, classify_input, validate_reply
from .schemas import LLM_OUTPUT_JSON_SCHEMA, AgentReply, LLMOutput
from .sessions import Exchange

DEFAULT_EMERGENCY_TEXT = "Please press your help button or call out for someone now."
DEFAULT_SELF_HARM_TEXT = ("I'm really glad you told me. Please tell someone near you right now, "
                          "or call or text 9-8-8 to talk to someone.")


@dataclass
class Prompt:
    version: str
    body: str
    sha256: str
    path: str


def load_prompt(path: Path) -> Prompt:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.S)
    if not m:
        raise ValueError(f"{path} has no front matter")
    front, body = m.group(1), m.group(2).strip()
    vm = re.search(r"^prompt_version:\s*(\S+)", front, flags=re.M)
    if not vm:
        raise ValueError("prompt_version missing from front matter")
    return Prompt(version=vm.group(1), body=body, sha256=hashlib.sha256(text.encode()).hexdigest()[:16], path=str(path))


def build_instructions(prompt: Prompt, ctx: ApprovedContext, screen: InputScreen) -> str:
    parts = [prompt.body, "# VERIFIED CAREGIVER CONTEXT\n\n" + ctx.to_prompt()]
    notes = ["The person's words come from automatic speech recognition and may contain recognition errors."]
    if screen.kind == "leaving":
        notes.append("The person may be talking about leaving or going somewhere. Validate the feeling, do not "
                     "agree with leaving, offer a safe alternative, and set safety_flag to notify_caregiver.")
    parts.append("# NOTES FOR THIS TURN\n\n" + "\n".join(f"- {n}" for n in notes))
    return "\n\n".join(parts)


def build_messages(history: list[Exchange], transcript: str) -> list[dict]:
    msgs: list[dict] = []
    for ex in history:
        msgs.append({"role": "user", "content": ex.user})
        msgs.append({"role": "assistant", "content": ex.assistant})
    msgs.append({"role": "user", "content": transcript})
    return msgs


@dataclass
class AgentResult:
    reply: AgentReply
    report: ValidationReport
    llm: LLMResult | None
    llm_ms: float
    error_code: str | None = None


class Agent:
    def __init__(self, llm: LLMProvider, prompt: Prompt):
        self.llm, self.prompt = llm, prompt

    async def respond(self, transcript: str, history: list[Exchange], ctx: ApprovedContext,
                      emergency_text: str = DEFAULT_EMERGENCY_TEXT,
                      self_harm_text: str = DEFAULT_SELF_HARM_TEXT) -> AgentResult:
        screen = classify_input(transcript)
        instructions = build_instructions(self.prompt, ctx, screen)
        messages = build_messages(history, transcript)
        t0 = time.perf_counter()
        raw: LLMOutput | None = None
        llm_res: LLMResult | None = None
        err: str | None = None
        try:
            llm_res = await self.llm.generate(instructions, messages, LLM_OUTPUT_JSON_SCHEMA)
            if llm_res.raw_json:
                raw = LLMOutput.model_validate(json.loads(llm_res.raw_json))
            else:
                err = f"empty_output:{llm_res.incomplete_reason or 'none'}"
        except (json.JSONDecodeError, ValidationError):
            err = "invalid_json"
        except ProviderError:
            raise
        llm_ms = (time.perf_counter() - t0) * 1000
        reply, report = validate_reply(raw, transcript, screen, ctx.terms(), ctx.voice_ids(),
                                       emergency_text, self_harm_text, self.prompt.version)
        reply.latency_ms.llm = round(llm_ms, 1)
        return AgentResult(reply=reply, report=report, llm=llm_res, llm_ms=llm_ms, error_code=err)
