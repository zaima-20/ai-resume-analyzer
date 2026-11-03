"""Tests for src/parser.py. PDF reading is mocked; DOCX tests use the sample resume."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import parser
from src.config import MAX_FILE_SIZE_MB
from src.parser import (
    ResumeParseError,
    detect_sections,
    extract_contact_info,
    find_section_name,
    parse_resume,
    parse_resume_file,
)

SAMPLE_DOCX = Path(__file__).parent.parent / "sample_data" / "sample_resume.docx"


def mock_pdfplumber(monkeypatch: pytest.MonkeyPatch, page_texts: list[str | None]) -> None:
    """Replace pdfplumber.open so it returns fake pages with the given texts."""
    pages = [MagicMock(**{"extract_text.return_value": text}) for text in page_texts]
    fake_pdf = MagicMock()
    fake_pdf.__enter__.return_value.pages = pages
    monkeypatch.setattr(parser.pdfplumber, "open", MagicMock(return_value=fake_pdf))


# --- DOCX: the sample resume ---


def test_sample_docx_finds_all_sections() -> None:
    resume = parse_resume_file(SAMPLE_DOCX)
    assert list(resume.sections) == [
        "Summary", "Experience", "Education", "Skills", "Projects", "Certifications",
    ]


def test_sample_docx_section_content() -> None:
    resume = parse_resume_file(SAMPLE_DOCX)
    assert "SQL, Python, Excel" in resume.sections["Skills"]
    assert "Example University" in resume.sections["Education"]
    # The name and contact line come before the first heading, so they aren't in any section.
    assert all("Jordan Example" not in text for text in resume.sections.values())


def test_sample_docx_contact_details() -> None:
    contact = parse_resume_file(SAMPLE_DOCX).contact
    assert contact.email == "jordan.example@example.com"
    assert contact.phone == "+1 (555) 010-4477"
    assert contact.linkedin == "linkedin.com/in/jordan-example-demo"
    assert contact.github == "github.com/jordan-example-demo"


def test_sample_docx_word_count() -> None:
    resume = parse_resume_file(SAMPLE_DOCX)
    assert resume.word_count == len(resume.text.split())
    assert resume.word_count > 100


def test_damaged_docx_raises_error() -> None:
    with pytest.raises(ResumeParseError, match="could not be opened"):
        parse_resume(b"this is not a real docx file", "resume.docx")


# --- PDF (pdfplumber is mocked) ---


def test_pdf_pages_are_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_pdfplumber(monkeypatch, ["Sam Sample\nSkills\nPython", "Education\nBS Physics"])
    resume = parse_resume(b"%PDF-fake", "resume.pdf")
    assert resume.sections == {"Skills": "Python", "Education": "BS Physics"}


def test_scanned_pdf_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scanned pages have no text layer, so pdfplumber returns None.
    mock_pdfplumber(monkeypatch, [None, None])
    with pytest.raises(ResumeParseError, match="scanned image"):
        parse_resume(b"%PDF-fake", "scan.pdf")


def test_whitespace_only_pdf_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_pdfplumber(monkeypatch, ["   \n  "])
    with pytest.raises(ResumeParseError, match="No readable text"):
        parse_resume(b"%PDF-fake", "blank.pdf")


def test_damaged_pdf_raises_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parser.pdfplumber, "open", MagicMock(side_effect=ValueError("bad")))
    with pytest.raises(ResumeParseError, match="could not be opened"):
        parse_resume(b"%PDF-fake", "broken.pdf")


def test_uppercase_pdf_extension_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_pdfplumber(monkeypatch, ["Summary\nHello"])
    assert parse_resume(b"%PDF-fake", "RESUME.PDF").sections == {"Summary": "Hello"}


# --- File checks ---


@pytest.mark.parametrize("filename", ["resume.txt", "resume.doc", "resume"])
def test_unsupported_file_type(filename: str) -> None:
    with pytest.raises(ResumeParseError, match="Unsupported file type"):
        parse_resume(b"some bytes", filename)


def test_file_too_large() -> None:
    too_big = b"0" * (MAX_FILE_SIZE_MB * 1024 * 1024 + 1)
    with pytest.raises(ResumeParseError, match=f"{MAX_FILE_SIZE_MB} MB"):
        parse_resume(too_big, "resume.pdf")


def test_empty_file() -> None:
    with pytest.raises(ResumeParseError, match="empty"):
        parse_resume(b"", "resume.docx")


# --- Section detection ---


def test_heading_variants_are_recognised() -> None:
    text = "WORK EXPERIENCE:\nJob A\n  Technical Skills  \nSQL\nLicenses & Certifications\nAWS"
    assert detect_sections(text) == {
        "Experience": "Job A",
        "Skills": "SQL",
        "Certifications": "AWS",
    }


def test_heading_word_inside_a_sentence_is_not_a_heading() -> None:
    text = "Skills\nStrong communication skills\nExperience with SQL"
    assert detect_sections(text) == {
        "Skills": "Strong communication skills\nExperience with SQL"
    }


def test_repeated_heading_is_merged() -> None:
    text = "Projects\nApp one\nEducation\nBS\nProjects\nApp two"
    assert detect_sections(text)["Projects"] == "App one\nApp two"


def test_key_projects_and_research_heading() -> None:
    text = "KEY PROJECTS & RESEARCH\nThesis on solar forecasting"
    assert detect_sections(text) == {"Projects": "Thesis on solar forecasting"}


def test_areas_of_expertise_heading() -> None:
    text = "AREAS OF EXPERTISE\nData cleaning, reporting"
    assert detect_sections(text) == {"Skills": "Data cleaning, reporting"}


@pytest.mark.parametrize(
    ("line", "section"),
    [
        ("Areas of Expertise", "Skills"),
        ("Projects and Research", "Projects"),
        ("Relevant Work Experience:", "Experience"),
        ("PROFESSIONAL EXPERIENCE & INTERNSHIPS", "Experience"),
        ("Education & Training", "Education"),
        ("Courses and Certificates", "Certifications"),
    ],
)
def test_flexible_headings(line: str, section: str) -> None:
    assert find_section_name(line) == section


@pytest.mark.parametrize(
    "line",
    [
        "Strong communication skills",  # sentence case, not a heading
        "Experience with SQL",  # keyword followed by "with", not "and"
        "User Experience Designer",  # job title: keyword isn't the last word
        "Led 3 Client Projects",  # contains a digit
        "My Key Projects.",  # sentence punctuation
        "Some Of My Many Technical Skills Here",  # too many words
        "Project Manager",  # "project" is not "projects"
    ],
)
def test_sentences_and_job_titles_are_not_headings(line: str) -> None:
    assert find_section_name(line) is None


def test_two_headings_for_same_section_are_joined() -> None:
    text = (
        "Technical Skills\nSQL, Python\n"
        "Experience\nAnalyst at Demo Co.\n"
        "AREAS OF EXPERTISE\nData cleaning"
    )
    sections = detect_sections(text)
    assert sections["Skills"] == "SQL, Python\nData cleaning"
    assert sections["Experience"] == "Analyst at Demo Co."


def test_no_headings_gives_no_sections() -> None:
    assert detect_sections("Just a paragraph of text.") == {}


# --- Contact details ---


def test_contact_with_full_urls() -> None:
    contact = extract_contact_info(
        "Mail: a.b+jobs@mail.example.org\n"
        "https://www.linkedin.com/in/a-b-123/ and https://github.com/ab-dev"
    )
    assert contact.email == "a.b+jobs@mail.example.org"
    assert contact.linkedin == "https://www.linkedin.com/in/a-b-123/"
    assert contact.github == "https://github.com/ab-dev"


def test_dates_are_not_mistaken_for_phone_numbers() -> None:
    # A fictional number: area code 555 isn't assignable and no exchange starts with 0.
    text = "BS Computer Science | 2019 - 2023\n2024 - Present\nCall +1 (555) 010-0199"
    assert extract_contact_info(text).phone == "+1 (555) 010-0199"


def test_missing_contact_details_are_none() -> None:
    contact = extract_contact_info("No contact details here, graduated 2019 - 2023.")
    assert contact.email is None
    assert contact.phone is None
    assert contact.linkedin is None
    assert contact.github is None
