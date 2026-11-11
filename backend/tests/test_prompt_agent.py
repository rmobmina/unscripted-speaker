import asyncio
import json

from app.agent import Agent, build_instructions, load_prompt
from app.config import Settings
from app.context import ApprovedContext
from app.providers.mock import MockLLM
from app.safety import classify_input


def prompt():
    return load_prompt(Settings(_env_file=None).prompt_path)


def test_prompt_has_version_and_rules():
    p = prompt()
    assert p.version == "v1.0" and len(p.sha256) == 16
    assert "three" in p.body.lower() or "3" in p.body


def test_empty_context_instruction():
    ins = build_instructions(prompt(), ApprovedContext(), classify_input("hi"))
    assert "No caregiver-approved personal context" in ins


def test_context_rendered_and_leaving_note():
    ctx = ApprovedContext(preferred_name="Margaret",
                          people=[{"name": "Anna", "relationship": "daughter", "status": "living", "notes": ""}])
    ins = build_instructions(prompt(), ctx, classify_input("I need to go home"))
    assert "Anna, daughter" in ins and "Margaret" in ins and "notify_caregiver" in ins
    assert {"anna", "daughter", "margaret"} <= ctx.terms()


def test_agent_handles_invalid_json():
    llm = MockLLM(reply="not json at all")
    res = asyncio.run(Agent(llm, prompt()).respond("hello", [], ApprovedContext()))
    assert res.error_code == "invalid_json" and res.report.replaced


def test_agent_handles_schema_violation():
    llm = MockLLM(reply=json.dumps({"reply_text": "hi", "topic": "weather"}))
    res = asyncio.run(Agent(llm, prompt()).respond("hello", [], ApprovedContext()))
    assert res.error_code == "invalid_json"
