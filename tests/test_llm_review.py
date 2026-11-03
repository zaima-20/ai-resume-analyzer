"""Tests for src/llm_review.py. The Gemini client is fully mocked: no real API calls."""

import json
from datetime import date
from unittest.mock import MagicMock

import pytest
from google.genai import errors

from src import config, llm_review
from src.llm_review import LLMReview, LLMReviewError, build_prompt, parse_review, review_resume
from src.parser import ContactInfo, ParsedResume
from src.profile import UserProfile
from src.rule_checks import WEAK_PHRASES

RESUME = ParsedResume(
    text="Sam Sample\nExperience\nWorked on sales reports\nSkills\nSQL, Excel",
    sections={"Experience": "Worked on sales reports", "Skills": "SQL, Excel"},
    contact=ContactInfo(email="sam@example.com"),
)

VALID_REPLY: dict = {
    "overall_score": 62,
    "ats_score": 70,
    "strengths": ["Clear skills section"],
    "problems": [
        {
            "issue": "Bullets have no results",
            "why_it_matters": "Recruiters look for impact",
            "fix": "Add numbers to each bullet",
        }
    ],
    "rewritten_bullets": [
        {"original": "Worked on sales reports", "improved": "Built [N] weekly sales reports"},
    ],
    "missing_keywords": ["Power BI", "Data cleaning"],
}


def fake_response(text: str) -> MagicMock:
    """A stand-in for a Gemini response object with the given .text."""
    return MagicMock(text=text)


@pytest.fixture(autouse=True)
def block_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no test can create a real Gemini client by accident."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("A real Gemini client was created in a test")

    monkeypatch.setattr(llm_review.genai, "Client", fail)


@pytest.fixture
def fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend a Gemini key is set, without touching the real .env key."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "fake-test-key")


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch, fake_key: None) -> MagicMock:
    """Replace genai.Client with a mock. Set .models.generate_content.side_effect in tests."""
    client = MagicMock()
    monkeypatch.setattr(llm_review.genai, "Client", MagicMock(return_value=client))
    return client


# --- review_resume() ---


def test_valid_json_returns_review(fake_client: MagicMock) -> None:
    fake_client.models.generate_content.side_effect = [fake_response(json.dumps(VALID_REPLY))]

    review = review_resume(RESUME)

    assert isinstance(review, LLMReview)
    assert review.overall_score == 62
    assert review.ats_score == 70
    assert review.problems[0].why_it_matters == "Recruiters look for impact"
    assert review.rewritten_bullets[0].improved == "Built [N] weekly sales reports"
    assert review.missing_keywords == ["Power BI", "Data cleaning"]
    assert fake_client.models.generate_content.call_count == 1


def test_request_uses_json_mode_and_configured_model(fake_client: MagicMock) -> None:
    fake_client.models.generate_content.side_effect = [fake_response(json.dumps(VALID_REPLY))]

    review_resume(RESUME)

    kwargs = fake_client.models.generate_content.call_args.kwargs
    assert kwargs["model"] == config.GEMINI_MODEL
    assert kwargs["config"].response_mime_type == "application/json"
    assert kwargs["config"].response_json_schema == llm_review.RESPONSE_SCHEMA
    assert "Worked on sales reports" in kwargs["contents"]


def test_invalid_json_then_retry_succeeds(fake_client: MagicMock) -> None:
    fake_client.models.generate_content.side_effect = [
        fake_response("Sure! Here is your review: {not json"),
        fake_response(json.dumps(VALID_REPLY)),
    ]

    review = review_resume(RESUME)

    assert review.overall_score == 62
    assert fake_client.models.generate_content.call_count == 2
    # The retry tells Gemini what was wrong with the first reply.
    retry_prompt = fake_client.models.generate_content.call_args.kwargs["contents"]
    assert "not valid JSON" in retry_prompt


def test_invalid_twice_raises_error(fake_client: MagicMock) -> None:
    bad_score = {**VALID_REPLY, "overall_score": 150}
    fake_client.models.generate_content.side_effect = [
        fake_response("not json"),
        fake_response(json.dumps(bad_score)),
    ]

    with pytest.raises(LLMReviewError, match="unexpected format"):
        review_resume(RESUME)
    assert fake_client.models.generate_content.call_count == 2


def test_empty_reply_is_retried(fake_client: MagicMock) -> None:
    fake_client.models.generate_content.side_effect = [
        fake_response(None),
        fake_response(json.dumps(VALID_REPLY)),
    ]
    assert review_resume(RESUME).ats_score == 70


@pytest.mark.parametrize("value", [None, "", "your_gemini_api_key_here"])
def test_missing_key_raises_friendly_error(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", value)

    # block_real_client would raise AssertionError if a client were created.
    with pytest.raises(LLMReviewError, match="GEMINI_API_KEY"):
        review_resume(RESUME)


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (429, "Resource exhausted", "free-tier limit"),
        (400, "API key not valid. Please pass a valid API key.", "rejected the API key"),
        (404, "models/x is not found", "was not found"),
        (503, "The model is overloaded", "try again in a few minutes"),
    ],
)
def test_api_errors_are_friendly_and_not_retried(
    fake_client: MagicMock, code: int, message: str, expected: str
) -> None:
    error = errors.APIError(code, {"error": {"code": code, "message": message}})
    fake_client.models.generate_content.side_effect = error

    with pytest.raises(LLMReviewError, match=expected):
        review_resume(RESUME)
    assert fake_client.models.generate_content.call_count == 1


# --- build_prompt() ---


def test_prompt_includes_profile_title_and_skills() -> None:
    profile = UserProfile(target_title="Data Analyst", skills={"SQL": 4, "Tableau": 2})
    prompt = build_prompt(RESUME, profile)
    assert "Target job title: Data Analyst" in prompt
    assert "SQL (4/5), Tableau (2/5)" in prompt
    assert RESUME.text in prompt


def test_prompt_without_profile() -> None:
    assert "Target job title: not given" in build_prompt(RESUME)


def test_prompt_contains_given_date() -> None:
    prompt = build_prompt(RESUME, today=date(2026, 11, 3))
    assert prompt.startswith("Today's date: 2026-11-03 (03 November 2026)")


def test_prompt_defaults_to_today() -> None:
    assert date.today().isoformat() in build_prompt(RESUME)


def test_request_sends_todays_date(fake_client: MagicMock) -> None:
    fake_client.models.generate_content.side_effect = [fake_response(json.dumps(VALID_REPLY))]
    review_resume(RESUME)
    contents = fake_client.models.generate_content.call_args.kwargs["contents"]
    assert date.today().isoformat() in contents


def test_system_instruction_says_to_judge_dates_against_today() -> None:
    assert "Judge every date in the resume relative to it" in llm_review.SYSTEM_INSTRUCTION


def test_system_instruction_lists_every_weak_phrase() -> None:
    for phrase in WEAK_PHRASES:
        assert f'"{phrase}"' in llm_review.SYSTEM_INSTRUCTION


def test_request_sends_weak_phrases_and_keep_facts_rule(fake_client: MagicMock) -> None:
    fake_client.models.generate_content.side_effect = [fake_response(json.dumps(VALID_REPLY))]
    review_resume(RESUME)
    instruction = fake_client.models.generate_content.call_args.kwargs["config"].system_instruction
    assert '"responsible for"' in instruction
    assert "keep every fact from the original" in instruction


def test_system_instruction_forbids_inventing_facts() -> None:
    assert "Never invent" in llm_review.SYSTEM_INSTRUCTION
    assert "[X%]" in llm_review.SYSTEM_INSTRUCTION


# --- parse_review() ---


def test_parse_review_strips_code_fences() -> None:
    review = parse_review("```json\n" + json.dumps(VALID_REPLY) + "\n```")
    assert review.overall_score == 62


def test_parse_review_keeps_only_three_bullets() -> None:
    bullet = {"original": "a", "improved": "b"}
    review = parse_review(json.dumps({**VALID_REPLY, "rewritten_bullets": [bullet] * 5}))
    assert len(review.rewritten_bullets) == 3


def test_parse_review_accepts_whole_number_float_score() -> None:
    assert parse_review(json.dumps({**VALID_REPLY, "ats_score": 70.0})).ats_score == 70


@pytest.mark.parametrize(
    "bad_reply",
    [
        {**VALID_REPLY, "overall_score": -1},
        {**VALID_REPLY, "ats_score": "high"},
        {**VALID_REPLY, "ats_score": 70.5},
        {**VALID_REPLY, "strengths": "Clear skills section"},
        {**VALID_REPLY, "problems": [{"issue": "x", "fix": "y"}]},
        {**VALID_REPLY, "rewritten_bullets": [{"original": "x"}]},
        {key: value for key, value in VALID_REPLY.items() if key != "missing_keywords"},
        ["not", "an", "object"],
    ],
)
def test_parse_review_rejects_wrong_shapes(bad_reply: object) -> None:
    with pytest.raises(ValueError):
        parse_review(json.dumps(bad_reply))
