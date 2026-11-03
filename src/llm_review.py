"""Gemini-powered resume review that returns structured JSON."""

import json
import sys
from dataclasses import dataclass, field
from datetime import date

from google import genai
from google.genai import errors, types

from src import config
from src.parser import ParsedResume, ResumeParseError, parse_resume_file
from src.profile import UserProfile
from src.rule_checks import WEAK_PHRASES

MAX_ATTEMPTS: int = 2  # first try + one retry if the reply isn't valid
NUM_BULLETS: int = 3
TEMPERATURE: float = 0.3  # low = more consistent answers between runs

SYSTEM_INSTRUCTION: str = f"""You are an experienced recruiter and resume reviewer.
Review the resume you are given and reply with JSON only, matching the schema.

Rules:
- Never invent experience, employers, numbers, skills or qualifications that are not in the resume.
- Base every strength and problem on what is actually written in the resume.
- The message tells you today's date. Judge every date in the resume relative to it.
  A date on or before today is not in the future, and "Present" means the role is ongoing.
- Pick {NUM_BULLETS} real bullet points from the resume that would benefit most from rewriting.
  Copy each one exactly as "original", then write an "improved" version.
- An improved bullet must keep every fact from the original (each task, tool, result
  and responsibility). Make it stronger, never shorter in content.
- Do not use these weak phrases in improved bullets, in any form (e.g. "help", "helping",
  "assisting"): {", ".join(f'"{phrase}"' for phrase in WEAK_PHRASES)}.
  Start each improved bullet with a strong action verb instead.
- If an improved bullet needs a number the resume doesn't give, use a placeholder such as
  [X%], [N users] or [$X] for the candidate to fill in. Never make up the number.
- overall_score rates the whole resume from 0 to 100.
- ats_score rates from 0 to 100 how well an applicant tracking system could read it and
  match it to the target role (clear headings, standard sections, relevant keywords).
- missing_keywords are important skills or terms for the target role that the resume
  doesn't mention. They are suggestions to add only if the candidate really has them."""

# JSON Schema for Gemini's JSON response mode. Gemini follows it, but we still
# validate the reply ourselves in parse_review() because it isn't guaranteed.
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "overall_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "ats_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["issue", "why_it_matters", "fix"],
            },
        },
        "rewritten_bullets": {
            "type": "array",
            "maxItems": NUM_BULLETS,
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "improved": {"type": "string"},
                },
                "required": ["original", "improved"],
            },
        },
        "missing_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overall_score", "ats_score", "strengths", "problems",
        "rewritten_bullets", "missing_keywords",
    ],
}


class LLMReviewError(Exception):
    """Raised when the AI review can't be done. The message is safe to show to the user."""


@dataclass
class Problem:
    """One problem the AI found, with why it matters and how to fix it."""

    issue: str
    why_it_matters: str
    fix: str


@dataclass
class RewrittenBullet:
    """A bullet from the resume and an improved version of it."""

    original: str
    improved: str


@dataclass
class LLMReview:
    """The validated AI review of a resume."""

    overall_score: int
    ats_score: int
    strengths: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    rewritten_bullets: list[RewrittenBullet] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)


# ---------- Building the request ----------


def build_prompt(
    resume: ParsedResume, profile: UserProfile | None = None, today: date | None = None
) -> str:
    """Build the message sent to Gemini: today's date, the target role (if known) and the resume.

    Args:
        resume: The parsed resume.
        profile: The questionnaire answers, if saved.
        today: The date to give the model. Defaults to date.today(); tests pass a fixed date.
    """
    today = today or date.today()
    # The model doesn't know the current date, so it would misjudge "Aug 2025 - Present".
    parts: list[str] = [f"Today's date: {today.isoformat()} ({today:%d %B %Y})"]
    if profile is not None:
        parts.append(f"Target job title: {profile.target_title or 'not given'}")
        if profile.skills:
            skills = ", ".join(f"{name} ({rating}/5)" for name, rating in profile.skills.items())
            parts.append(f"Skills the candidate says they have (self-rated): {skills}")
    else:
        parts.append("Target job title: not given. Review the resume for its apparent field.")

    parts.append(f"Resume text:\n\"\"\"\n{resume.text}\n\"\"\"")
    return "\n\n".join(parts)


# ---------- Validating the reply ----------


def require_type(value: object, expected: type, name: str) -> None:
    """Raise ValueError if `value` is not of the expected type."""
    if not isinstance(value, expected):
        raise ValueError(f"'{name}' should be {expected.__name__}")


def parse_score(data: dict, name: str) -> int:
    """Read a 0-100 score from the reply, allowing whole-number floats like 72.0."""
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ValueError(f"'{name}' should be a whole number")
    if not 0 <= value <= 100:
        raise ValueError(f"'{name}' should be between 0 and 100")
    return int(value)


def parse_string_list(data: dict, name: str) -> list[str]:
    """Read a list of strings from the reply."""
    values = data.get(name)
    require_type(values, list, name)
    for value in values:
        require_type(value, str, f"{name} item")
    return values


def parse_object_list(data: dict, name: str, keys: list[str]) -> list[dict]:
    """Read a list of objects that each have the given string keys."""
    items = data.get(name)
    require_type(items, list, name)
    for item in items:
        require_type(item, dict, f"{name} item")
        for key in keys:
            require_type(item.get(key), str, f"{name}.{key}")
    return items


def parse_review(raw_text: str) -> LLMReview:
    """Turn Gemini's JSON reply into an LLMReview.

    Raises:
        ValueError: If the text isn't valid JSON or doesn't match the expected shape.
    """
    text = raw_text.strip()
    # JSON mode shouldn't add ```json fences, but remove them just in case.
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"reply is not valid JSON ({error.msg})") from error
    require_type(data, dict, "reply")

    problems = parse_object_list(data, "problems", ["issue", "why_it_matters", "fix"])
    bullets = parse_object_list(data, "rewritten_bullets", ["original", "improved"])

    return LLMReview(
        overall_score=parse_score(data, "overall_score"),
        ats_score=parse_score(data, "ats_score"),
        strengths=parse_string_list(data, "strengths"),
        problems=[Problem(p["issue"], p["why_it_matters"], p["fix"]) for p in problems],
        rewritten_bullets=[
            RewrittenBullet(b["original"], b["improved"]) for b in bullets[:NUM_BULLETS]
        ],
        missing_keywords=parse_string_list(data, "missing_keywords"),
    )


# ---------- Calling Gemini ----------


def friendly_api_error(error: errors.APIError) -> str:
    """Turn a Gemini API error into a message the user can act on."""
    if error.code == 429:
        return "The Gemini free-tier limit was reached. Please wait a minute and try again."
    if error.code in (400, 401, 403) and "key" in str(error.message).lower():
        return "Gemini rejected the API key. Check GEMINI_API_KEY in your .env file."
    if error.code == 404:
        return f"The Gemini model '{config.GEMINI_MODEL}' was not found. Check GEMINI_MODEL in src/config.py."
    if error.code >= 500:
        return "Gemini is having problems right now. Please try again in a few minutes."
    return f"The Gemini request failed (error {error.code}). Please try again."


def review_resume(resume: ParsedResume, profile: UserProfile | None = None) -> LLMReview:
    """Ask Gemini to review a resume and return the validated result.

    If the reply isn't valid, the request is sent once more with a note about
    what was wrong.

    Args:
        resume: The resume from parser.parse_resume().
        profile: The questionnaire answers, used for the target role and skills.

    Returns:
        An LLMReview with scores, strengths, problems, rewritten bullets and keywords.

    Raises:
        LLMReviewError: If the key is missing, the API call fails, or the reply
            is still invalid after the retry.
    """
    if not config.has_key("GEMINI_API_KEY"):
        raise LLMReviewError(
            "The AI review needs a Gemini API key. Add GEMINI_API_KEY to your .env file "
            "(get a free key at https://aistudio.google.com/apikey)."
        )

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    request_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_json_schema=RESPONSE_SCHEMA,
        temperature=TEMPERATURE,
    )
    prompt = build_prompt(resume, profile)

    last_problem = ""
    for _attempt in range(MAX_ATTEMPTS):
        contents = prompt
        if last_problem:
            contents += (
                f"\n\nYour previous reply was rejected because the {last_problem}. "
                "Reply again with JSON that matches the schema exactly."
            )

        try:
            response = client.models.generate_content(
                model=config.GEMINI_MODEL, contents=contents, config=request_config
            )
        except errors.APIError as error:
            # Retrying straight away won't fix a bad key or a quota limit, so stop here.
            raise LLMReviewError(friendly_api_error(error)) from error

        try:
            return parse_review(response.text or "")
        except ValueError as error:
            last_problem = str(error)

    raise LLMReviewError(
        "The AI review came back in an unexpected format twice. Please try again."
    )


# Run from the command line:  python -m src.llm_review path/to/resume.pdf
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m src.llm_review path/to/resume.pdf")
        sys.exit(1)

    try:
        parsed = parse_resume_file(sys.argv[1])
        review = review_resume(parsed)
    except FileNotFoundError:
        print(f"File not found: {sys.argv[1]}")
        sys.exit(1)
    except (ResumeParseError, LLMReviewError) as error:
        print(f"Error: {error}")
        sys.exit(1)

    print(f"Overall score: {review.overall_score}/100")
    print(f"ATS score:     {review.ats_score}/100")

    print("\nStrengths:")
    for strength in review.strengths:
        print(f"  - {strength}")

    print("\nProblems:")
    for number, problem in enumerate(review.problems, start=1):
        print(f"  {number}. {problem.issue}")
        print(f"     Why it matters: {problem.why_it_matters}")
        print(f"     Fix: {problem.fix}")

    print("\nRewritten bullets:")
    for bullet in review.rewritten_bullets:
        print(f"  Before: {bullet.original}")
        print(f"  After:  {bullet.improved}\n")

    print("Missing keywords:", ", ".join(review.missing_keywords) or "none")
