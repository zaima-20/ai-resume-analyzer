"""Deterministic resume checks that run without an LLM."""

import re
import sys
from dataclasses import dataclass

from src.parser import ParsedResume, ResumeParseError, parse_resume_file
from src.profile import UserProfile

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
# Used to sort issues: high first, then medium, then low.
SEVERITY_ORDER: dict[str, int] = {HIGH: 0, MEDIUM: 1, LOW: 2}

# Sections every resume should have, with how serious it is when one is missing.
REQUIRED_SECTIONS: dict[str, str] = {
    "Experience": HIGH,
    "Education": HIGH,
    "Skills": HIGH,
    "Summary": MEDIUM,
    "Projects": MEDIUM,
}

MIN_WORDS: int = 300
MAX_WORDS: int = 900
WORDS_PER_PAGE: int = 450  # rough average for a resume page

# Only these sections are checked for bullets without numbers.
BULLET_SECTIONS: list[str] = ["Experience", "Projects"]
# Characters that start a bullet point in PDF text (DOCX bullets have no character).
BULLET_CHARS: str = "•●◦▪■►✓·*-–"
# A line without a bullet character counts as a bullet if it has at least this many
# words. Shorter lines are usually job titles or company names, not achievements.
MIN_BULLET_WORDS: int = 6
MAX_EXAMPLES: int = 3

WEAK_PHRASES: list[str] = [
    "responsible for",
    "worked on",
    "helped",
    "assisted with",
    "assisted in",
    "involved in",
    "participated in",
    "duties included",
    "tasked with",
]


@dataclass
class Issue:
    """One problem found in the resume.

    Attributes:
        issue: What is wrong, in one sentence.
        severity: "high", "medium" or "low".
        location: Where it was found, e.g. a section name or "Contact details".
        suggested_fix: What the user can do about it.
    """

    issue: str
    severity: str
    location: str
    suggested_fix: str


# ---------- Helpers ----------


def clean_line(line: str) -> str:
    """Remove a leading bullet character and surrounding spaces from a line."""
    return line.strip().lstrip(BULLET_CHARS).strip()


def resume_lines(resume: ParsedResume) -> list[tuple[str, str]]:
    """Return every non-empty line with the name of the section it belongs to.

    If no sections were found, all lines are returned under "Resume".
    """
    if not resume.sections:
        return [("Resume", line.strip()) for line in resume.text.splitlines() if line.strip()]
    return [
        (section, line)
        for section, text in resume.sections.items()
        for line in text.splitlines()
        if line.strip()
    ]


def contains_term(text: str, term: str) -> bool:
    """Return True if `term` appears in `text` as a whole word (case-insensitive).

    Works for skills with symbols too, e.g. "C++" or "Node.js".
    """
    pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


# ---------- Individual checks ----------


def check_sections(resume: ParsedResume) -> list[Issue]:
    """Report important sections that are missing."""
    issues: list[Issue] = []
    for section, severity in REQUIRED_SECTIONS.items():
        if section not in resume.sections:
            issues.append(
                Issue(
                    issue=f"No '{section}' section was found.",
                    severity=severity,
                    location=section,
                    suggested_fix=f"Add a clear '{section}' heading with its own content.",
                )
            )
    return issues


def check_contact(resume: ParsedResume) -> list[Issue]:
    """Report missing email, phone, LinkedIn or GitHub details."""
    contact = resume.contact
    checks = [
        ("email address", contact.email, HIGH),
        ("phone number", contact.phone, HIGH),
        ("LinkedIn profile link", contact.linkedin, MEDIUM),
        ("GitHub profile link", contact.github, MEDIUM),
    ]
    return [
        Issue(
            issue=f"No {name} was found.",
            severity=severity,
            location="Contact details",
            suggested_fix=f"Add your {name} at the top of the resume, next to your name.",
        )
        for name, value, severity in checks
        if not value
    ]


def check_length(resume: ParsedResume) -> list[Issue]:
    """Report a resume that is too short or too long."""
    words = resume.word_count
    pages = words / WORDS_PER_PAGE
    if words < MIN_WORDS:
        return [
            Issue(
                issue=f"The resume is short: {words} words (about {pages:.1f} pages).",
                severity=MEDIUM,
                location="Whole resume",
                suggested_fix=(
                    f"Aim for at least {MIN_WORDS} words. Add detail to your experience "
                    "and projects: what you did, the tools you used and the results."
                ),
            )
        ]
    if words > MAX_WORDS:
        return [
            Issue(
                issue=f"The resume is long: {words} words (about {pages:.1f} pages).",
                severity=MEDIUM,
                location="Whole resume",
                suggested_fix=(
                    f"Cut it to under {MAX_WORDS} words (about 2 pages). Remove old or "
                    "less relevant roles and shorten long bullets."
                ),
            )
        ]
    return []


def check_metrics(resume: ParsedResume) -> list[Issue]:
    """Report bullet points in Experience and Projects that contain no numbers."""
    bullets_without_numbers: list[str] = []
    sections_with_problems: list[str] = []
    for section in BULLET_SECTIONS:
        for raw_line in resume.sections.get(section, "").splitlines():
            line = clean_line(raw_line)
            starts_with_bullet = raw_line.strip()[:1] in BULLET_CHARS
            is_bullet = starts_with_bullet or len(line.split()) >= MIN_BULLET_WORDS
            if line and is_bullet and not any(char.isdigit() for char in line):
                bullets_without_numbers.append(line)
                if section not in sections_with_problems:
                    sections_with_problems.append(section)

    if not bullets_without_numbers:
        return []

    count = len(bullets_without_numbers)
    examples = "; ".join(f'"{line}"' for line in bullets_without_numbers[:MAX_EXAMPLES])
    return [
        Issue(
            issue=f"{count} bullet point(s) have no numbers or metrics, e.g. {examples}.",
            severity=MEDIUM,
            location=", ".join(sections_with_problems),
            suggested_fix=(
                "Show the result of your work with numbers: how many, how much, "
                "how fast or what percentage (e.g. 'cut report time by 30%')."
            ),
        )
    ]


def check_weak_phrases(resume: ParsedResume) -> list[Issue]:
    """Report each line that uses a weak phrase like "responsible for"."""
    issues: list[Issue] = []
    for section, raw_line in resume_lines(resume):
        line = clean_line(raw_line)
        for phrase in WEAK_PHRASES:
            if contains_term(line, phrase):
                issues.append(
                    Issue(
                        issue=f'Weak phrase "{phrase}" in: "{line}"',
                        severity=LOW,
                        location=section,
                        suggested_fix=(
                            "Start with a strong action verb that shows what you did, "
                            "such as Built, Led, Automated, Reduced or Improved."
                        ),
                    )
                )
                break  # one issue per line is enough
    return issues


def check_profile_skills(resume: ParsedResume, profile: UserProfile) -> list[Issue]:
    """Report skills from the questionnaire that never appear in the resume."""
    return [
        Issue(
            issue=f"You listed '{skill}' as a skill, but it isn't mentioned in your resume.",
            severity=MEDIUM,
            location="Skills",
            suggested_fix=(
                f"If you really have it, add '{skill}' to your Skills section and show "
                "where you used it in a project or job."
            ),
        )
        for skill in profile.skills
        if not contains_term(resume.text, skill)
    ]


# ---------- Main entry point ----------


def run_checks(resume: ParsedResume, profile: UserProfile | None = None) -> list[Issue]:
    """Run every rule check on a parsed resume.

    Args:
        resume: The resume from parser.parse_resume().
        profile: The questionnaire answers. If given, claimed skills are checked too.

    Returns:
        All issues found, sorted high -> medium -> low severity.
    """
    issues: list[Issue] = []
    issues += check_sections(resume)
    issues += check_contact(resume)
    issues += check_length(resume)
    issues += check_metrics(resume)
    issues += check_weak_phrases(resume)
    if profile is not None:
        issues += check_profile_skills(resume, profile)

    # sorted() is stable, so issues with the same severity keep the order above.
    return sorted(issues, key=lambda issue: SEVERITY_ORDER[issue.severity])


# Run from the command line:  python -m src.rule_checks path/to/resume.pdf
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m src.rule_checks path/to/resume.pdf")
        sys.exit(1)

    try:
        parsed = parse_resume_file(sys.argv[1])
    except FileNotFoundError:
        print(f"File not found: {sys.argv[1]}")
        sys.exit(1)
    except ResumeParseError as error:
        print(f"Error: {error}")
        sys.exit(1)

    found = run_checks(parsed)
    print(f"{len(found)} issue(s) found.\n")
    for number, item in enumerate(found, start=1):
        print(f"{number}. [{item.severity.upper()}] {item.issue}")
        print(f"   Where: {item.location}")
        print(f"   Fix:   {item.suggested_fix}\n")
