"""Tests for src/rule_checks.py. Uses the sample resume plus small hand-made resumes."""

from pathlib import Path

import pytest

from src.parser import ContactInfo, ParsedResume, parse_resume_file
from src.profile import UserProfile
from src.rule_checks import (
    HIGH,
    LOW,
    MEDIUM,
    SEVERITY_ORDER,
    Issue,
    check_contact,
    check_length,
    check_metrics,
    check_sections,
    check_weak_phrases,
    run_checks,
)

SAMPLE_DOCX = Path(__file__).parent.parent / "sample_data" / "sample_resume.docx"

FULL_CONTACT = ContactInfo(
    email="sam@example.com",
    phone="+1 555 010 2233",
    linkedin="linkedin.com/in/sam",
    github="github.com/sam",
)


@pytest.fixture(scope="module")
def sample_resume() -> ParsedResume:
    """The parsed sample resume, read once for all tests in this file."""
    return parse_resume_file(SAMPLE_DOCX)


def make_resume(sections: dict[str, str], contact: ContactInfo = FULL_CONTACT) -> ParsedResume:
    """Build a ParsedResume by hand, with text made from the given sections."""
    text = "\n".join(f"{name}\n{body}" for name, body in sections.items())
    return ParsedResume(text=text, sections=sections, contact=contact)


def issues_containing(issues: list[Issue], text: str) -> list[Issue]:
    """Return the issues whose message contains `text`."""
    return [issue for issue in issues if text in issue.issue]


# --- The sample resume ---


def test_sample_has_no_section_or_contact_issues(sample_resume: ParsedResume) -> None:
    assert check_sections(sample_resume) == []
    assert check_contact(sample_resume) == []


def test_sample_is_flagged_as_too_short(sample_resume: ParsedResume) -> None:
    issues = check_length(sample_resume)
    assert len(issues) == 1
    assert "151 words" in issues[0].issue


def test_sample_bullets_without_numbers(sample_resume: ParsedResume) -> None:
    issues = check_metrics(sample_resume)
    assert len(issues) == 1
    assert issues[0].issue.startswith("4 bullet point(s)")
    # Only the first 3 examples are shown.
    assert "Responsible for cleaning" in issues[0].issue
    assert "small website" not in issues[0].issue
    # Bullets with numbers and short job-title lines are not flagged.
    assert "Built 6 weekly" not in issues[0].issue
    assert "Operations Assistant" not in issues[0].issue
    assert issues[0].location == "Experience, Projects"


def test_sample_weak_phrases(sample_resume: ParsedResume) -> None:
    issues = check_weak_phrases(sample_resume)
    assert [(issue.location, issue.issue.split('"')[1]) for issue in issues] == [
        ("Experience", "responsible for"),
        ("Experience", "worked on"),
        ("Experience", "helped"),
        ("Projects", "worked on"),
    ]
    assert all(issue.severity == LOW for issue in issues)
    assert "Helped the manager prepare quarterly reports" in issues[2].issue


def test_sample_missing_profile_skill(sample_resume: ParsedResume) -> None:
    profile = UserProfile(
        target_title="Data Analyst",
        skills={"sql": 5, "Python": 4, "Tableau": 3, "Power BI": 2},
    )
    issues = run_checks(sample_resume, profile)
    missing = [i.issue for i in issues if "isn't mentioned" in i.issue]
    assert len(missing) == 2
    assert "'Tableau'" in missing[0]
    assert "'Power BI'" in missing[1]


def test_no_profile_skips_skill_check(sample_resume: ParsedResume) -> None:
    assert issues_containing(run_checks(sample_resume), "isn't mentioned") == []


# --- Sorting ---


def test_issues_are_sorted_by_severity() -> None:
    resume = make_resume({"Summary": "Worked on reports."}, contact=ContactInfo())
    issues = run_checks(resume)
    orders = [SEVERITY_ORDER[issue.severity] for issue in issues]
    assert orders == sorted(orders)
    assert issues[0].severity == HIGH
    assert issues[-1].severity == LOW


# --- Missing sections and contact details ---


def test_missing_sections_have_the_right_severity() -> None:
    issues = check_sections(make_resume({"Experience": "Job"}))
    by_section = {issue.location: issue.severity for issue in issues}
    assert by_section == {
        "Education": HIGH,
        "Skills": HIGH,
        "Summary": MEDIUM,
        "Projects": MEDIUM,
    }


def test_missing_contact_details() -> None:
    contact = ContactInfo(email="sam@example.com", github="github.com/sam")
    issues = check_contact(make_resume({}, contact=contact))
    assert [(i.issue, i.severity) for i in issues] == [
        ("No phone number was found.", HIGH),
        ("No LinkedIn profile link was found.", MEDIUM),
    ]


# --- Length ---


def test_good_length_has_no_issue() -> None:
    assert check_length(make_resume({"Experience": "word " * 500})) == []


def test_too_long_resume() -> None:
    issues = check_length(make_resume({"Experience": "word " * 1000}))
    assert len(issues) == 1
    assert "long" in issues[0].issue


# --- Bullets and weak phrases ---


def test_pdf_bullet_characters_are_recognised() -> None:
    # Short lines starting with a bullet character count as bullets too.
    resume = make_resume({"Experience": "• Led team meetings\n- Cut costs by 10%"})
    issues = check_metrics(resume)
    assert issues[0].issue.startswith("1 bullet point(s)")
    assert '"Led team meetings"' in issues[0].issue


def test_no_metrics_issue_when_all_bullets_have_numbers() -> None:
    resume = make_resume({"Experience": "Grew sales by 20% across 3 regions in a year"})
    assert check_metrics(resume) == []


def test_weak_phrase_inside_another_word_is_ignored() -> None:
    # "helpedesk" is not "helped"; "Helped" is matched regardless of case.
    resume = make_resume({"Experience": "Ran the helpedesk queue\nHELPED launch an app"})
    issues = check_weak_phrases(resume)
    assert len(issues) == 1
    assert "HELPED launch an app" in issues[0].issue


def test_one_issue_per_line_even_with_two_weak_phrases() -> None:
    resume = make_resume({"Projects": "Worked on and helped with a website"})
    assert len(check_weak_phrases(resume)) == 1


def test_weak_phrases_found_when_no_sections_detected() -> None:
    resume = ParsedResume(text="Sam Sample\nResponsible for the shop", contact=FULL_CONTACT)
    issues = check_weak_phrases(resume)
    assert len(issues) == 1
    assert issues[0].location == "Resume"


def test_skills_with_symbols_are_matched() -> None:
    resume = make_resume({"Skills": "C++, Node.js, SQL"})
    profile = UserProfile(skills={"c++": 3, "node.js": 3, "C#": 3})
    missing = issues_containing(run_checks(resume, profile), "isn't mentioned")
    assert len(missing) == 1
    assert "'C#'" in missing[0].issue
