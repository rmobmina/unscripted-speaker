import pytest

from app.agent import DEFAULT_EMERGENCY_TEXT as EM, DEFAULT_SELF_HARM_TEXT as SH
from app.safety import FALLBACK_REPLY, classify_input, validate_reply
from app.schemas import LLMOutput


def out(text, topic="general", conf=0.9, flag="none", ctx=False, vid=None):
    return LLMOutput(reply_text=text, topic=topic, topic_confidence=conf, uses_verified_context=ctx,
                     safety_flag=flag, voice_message_id=vid)


def run(raw, transcript="hello", terms=frozenset(), vids=frozenset()):
    return validate_reply(raw, transcript, classify_input(transcript), set(terms), set(vids), EM, SH, "v1.0")


@pytest.mark.parametrize("t", ["I fell down and can't get up", "I've fallen", "my chest hurts", "I can't breathe",
                               "there's a fire in the kitchen", "I smell smoke", "I'm bleeding", "I'm in a lot of pain",
                               "I took too many pills", "call an ambulance", "help me, I'm hurt"])
def test_emergency_detected(t):
    assert classify_input(t).flag == "emergency_guidance"


@pytest.mark.parametrize("t", ["I want to die", "I want to kill myself", "I don't want to live anymore"])
def test_self_harm_detected(t):
    s = classify_input(t)
    assert s.flag == "emergency_guidance" and s.kind == "self_harm"


@pytest.mark.parametrize("t", ["I need to go home", "take me home", "I have to go to work", "I'll drive to the shop",
                               "I need to pick up the kids"])
def test_leaving_detected(t):
    assert classify_input(t).flag == "notify_caregiver"


@pytest.mark.parametrize("t", ["I'm sitting by the fire", "where is my blood pressure pill", "can you help me remember",
                               "I watched a film about a stroke of luck", "the pain of missing her", "hello",
                               "what day is it", "I miss my husband", "is it lunch time"])
def test_benign_not_emergency(t):
    assert classify_input(t).flag != "emergency_guidance", t


def test_emergency_template_overrides_model():
    rep, r = run(out("Oh dear, let's have some tea!"), "I fell and can't get up")
    assert rep.reply_text == EM and rep.safety_flag == "emergency_guidance" and r.replaced


def test_self_harm_template():
    rep, _ = run(out("ok"), "I want to die")
    assert rep.reply_text == SH


def test_model_cannot_lower_flag_but_can_raise():
    rep, _ = run(out("I'm here.", flag="none"), "I fell down")
    assert rep.safety_flag == "emergency_guidance"
    rep, _ = run(out("I hear you.", flag="notify_caregiver"), "hmm")
    assert rep.safety_flag == "notify_caregiver"


@pytest.mark.parametrize("bad", ["You already asked me that.", "Okay sweetie, it's fine.", "I am your daughter.",
                                 "I've called your son.", "She is on her way.", "He will be home soon.",
                                 "That's because of your dementia.", "Do you remember what her name is?",
                                 "I'm a real person."])
def test_banned_outputs_replaced(bad):
    rep, r = run(out(bad))
    assert rep.reply_text == FALLBACK_REPLY and r.replaced


def test_ungrounded_name_replaced_and_grounded_allowed():
    rep, r = run(out("Your son Michael loves you."))
    assert r.replaced and "ungrounded_name" in r.rules
    rep, r = run(out("Your son Michael loves you."), terms={"michael", "son"})
    assert not r.replaced and rep.uses_verified_context
    rep, r = run(out("It sounds like Robert matters a lot to you."), transcript="where is Robert")
    assert not r.replaced and not rep.uses_verified_context


def test_question_and_sentence_limits():
    rep, r = run(out("I'm here. Are you warm? Shall we sit? It's nice."))
    assert rep.reply_text == "I'm here. Are you warm?" and r.trimmed_questions
    rep, r = run(out("One. Two. Three. Four. Five."))
    assert rep.reply_text == "One. Two. Three." and r.trimmed_sentences


def test_low_confidence_topic_falls_back():
    rep, _ = run(out("I'm here with you.", topic="family", conf=0.5))
    assert rep.topic == "general"
    rep, _ = run(out("I'm here with you.", topic="family", conf=0.6))
    assert rep.topic == "family"


def test_voice_message_must_be_approved():
    rep, _ = run(out("Here it is.", vid="vm_1"), vids={"vm_2"})
    assert rep.voice_message_id is None
    rep, _ = run(out("Here it is.", vid="vm_2"), vids={"vm_2"})
    assert rep.voice_message_id == "vm_2"


def test_invalid_model_output_fallback():
    rep, r = run(None)
    assert rep.reply_text == FALLBACK_REPLY and "invalid_model_output" in r.rules
    rep, _ = run(None, "I fell down")
    assert rep.reply_text == EM


def test_markdown_stripped():
    rep, _ = run(out("**I'm** here with you."))
    assert "*" not in rep.reply_text
