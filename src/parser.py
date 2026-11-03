"""Extracts text, sections and contact details from PDF and DOCX resumes."""

import io
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import docx
import pdfplumber
from docx.table import Table

from src.config import MAX_FILE_SIZE_MB

SUPPORTED_EXTENSIONS: list[str] = [".pdf", ".docx"]

# Each section name maps to the headings that mean that section.
# A line that matches one of these exactly (ignoring case, a trailing colon
# and extra spaces) is always a heading.
SECTION_KEYWORDS: dict[str, list[str]] = {
    "Summary": [
        "summary", "professional summary", "career summary", "profile",
        "professional profile", "about me", "objective", "career objective",
    ],
    "Experience": [
        "experience", "work experience", "professional experience",
        "employment history", "work history", "employment",
    ],
    "Education": ["education", "academic background", "education and training"],
    "Skills": [
        "skills", "technical skills", "core skills", "key skills",
        "core competencies", "skills and tools",
    ],
    "Projects": ["projects", "personal projects", "key projects", "academic projects"],
    "Certifications": [
        "certifications", "certificates", "certification",
        "licenses and certifications", "certifications and licenses",
    ],
}

# Single words that mark a heading we don't know exactly, e.g. "KEY PROJECTS & RESEARCH"
# or "Areas of Expertise". See looks_like_heading() and find_section_name() for the rules.
# "profile" and "summary" are only matched exactly (above), because project names like
# "Sales Summary Dashboard" would otherwise be mistaken for headings.
HEADING_WORDS: dict[str, set[str]] = {
    "Summary": {"objective"},
    "Experience": {"experience", "employment"},
    "Education": {"education"},
    "Skills": {"skills", "expertise", "competencies"},
    "Projects": {"projects"},
    "Certifications": {"certifications", "certificates", "licenses"},
}
MAX_HEADING_WORDS: int = 5
SENTENCE_PUNCTUATION: str = ".,;!?"
# Short words that may stay lowercase in a Title Case heading ("Areas of Expertise").
SMALL_WORDS: set[str] = {"and", "of", "the", "for", "in"}

# --- Regex patterns for contact details ---
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Digits with optional +, spaces, dashes, dots or brackets in between (on one line).
PHONE_PATTERN = re.compile(r"\+?\d[\d \t().-]{7,}\d")
LINKEDIN_PATTERN = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.IGNORECASE
)
GITHUB_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?", re.IGNORECASE)
# Real phone numbers have 10-15 digits; this filters out dates like "2019 - 2021".
MIN_PHONE_DIGITS: int = 10
MAX_PHONE_DIGITS: int = 15


class ResumeParseError(Exception):
    """Raised when a resume can't be read. The message is safe to show to the user."""


@dataclass
class ContactInfo:
    """Contact details found in the resume. Each is None if not found."""

    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    github: str | None = None


@dataclass
class ParsedResume:
    """Everything the parser extracted from one resume.

    Attributes:
        text: The full plain text of the resume.
        sections: Section name (e.g. "Skills") -> the text under that heading.
            Only sections that were found are included.
        contact: Email, phone, LinkedIn and GitHub details.
    """

    text: str
    sections: dict[str, str] = field(default_factory=dict)
    contact: ContactInfo = field(default_factory=ContactInfo)

    @property
    def word_count(self) -> int:
        """Number of words in the resume text."""
        return len(self.text.split())


# ---------- File checks ----------


def check_file(filename: str, size_bytes: int) -> str:
    """Make sure the file type is supported and the file isn't too big.

    Args:
        filename: Original file name, used to read the extension.
        size_bytes: File size in bytes.

    Returns:
        The lowercase file extension, e.g. ".pdf".

    Raises:
        ResumeParseError: If the type is unsupported, the file is empty or too large.
    """
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ResumeParseError(
            f"Unsupported file type '{extension or 'none'}'. Please upload a PDF or DOCX file."
        )
    if size_bytes == 0:
        raise ResumeParseError("The file is empty.")
    if size_bytes > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise ResumeParseError(
            f"The file is larger than {MAX_FILE_SIZE_MB} MB. Please upload a smaller file."
        )
    return extension


# ---------- Text extraction ----------


def extract_pdf_text(file_bytes: bytes) -> str:
    """Return the text of every page in a PDF, one page after another.

    Raises:
        ResumeParseError: If the PDF is damaged or can't be opened.
    """
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            # extract_text() returns None for pages with no text (e.g. scanned images).
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as error:  # pdfplumber raises several error types for bad files
        raise ResumeParseError("This PDF could not be opened. It may be damaged.") from error
    return "\n".join(pages)


def extract_docx_text(file_bytes: bytes) -> str:
    """Return the text of a DOCX file, including text inside tables.

    Paragraphs and tables are read in the order they appear in the document,
    because many resume templates put whole sections inside tables.

    Raises:
        ResumeParseError: If the file isn't a valid DOCX.
    """
    try:
        document = docx.Document(io.BytesIO(file_bytes))
    except Exception as error:  # python-docx raises different errors for bad files
        raise ResumeParseError("This DOCX file could not be opened. It may be damaged.") from error

    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                for cell in row.cells:
                    lines.append(cell.text)
        else:
            lines.append(block.text)
    return "\n".join(lines)


# ---------- Sections and contact details ----------


def normalize_heading(line: str) -> str:
    """Turn a line into a simple form for comparing with heading keywords.

    Example: "  WORK EXPERIENCE: " -> "work experience", "Skills & Tools" -> "skills and tools".
    """
    line = line.strip().rstrip(":").replace("&", " and ").lower()
    return " ".join(line.split())  # collapse repeated spaces


def looks_like_heading(line: str) -> bool:
    """Return True if a line is styled like a heading rather than a sentence.

    A heading here means: at most MAX_HEADING_WORDS words, no sentence punctuation
    or digits, and written in ALL CAPS or Title Case.
    "Areas of Expertise" passes; "Strong communication skills" does not.
    """
    stripped = line.strip().rstrip(":").strip()
    words = stripped.split()
    if not words or len(words) > MAX_HEADING_WORDS:
        return False
    if any(char in SENTENCE_PUNCTUATION or char.isdigit() for char in stripped):
        return False
    if stripped.isupper():
        return True
    # Title Case: every word starts with a capital, except small words and symbols like "&".
    return all(
        word[0].isupper() or word.lower() in SMALL_WORDS or not word[0].isalpha()
        for word in words
    )


def find_section_name(line: str) -> str | None:
    """Return the section name if this line is a section heading, otherwise None.

    First tries an exact match against SECTION_KEYWORDS. If that fails, a line that
    looks like a heading still counts when it contains a word from HEADING_WORDS that
    is the last word or is followed by "and"/"&". That accepts "KEY PROJECTS & RESEARCH"
    but rejects job titles like "User Experience Designer" and "Experience with SQL".
    """
    heading = normalize_heading(line)
    for section, keywords in SECTION_KEYWORDS.items():
        if heading in keywords:
            return section

    if not looks_like_heading(line):
        return None

    words = heading.split()  # normalize_heading already turned "&" into "and"
    for index, word in enumerate(words):
        is_last = index == len(words) - 1
        if is_last or words[index + 1] == "and":
            for section, keyword_words in HEADING_WORDS.items():
                if word in keyword_words:
                    return section
    return None


def detect_sections(text: str) -> dict[str, str]:
    """Split resume text into sections using heading keyword matching.

    Text before the first heading (usually the name and contact line) is skipped.
    If two headings map to the same section (e.g. "Technical Skills" and
    "Areas of Expertise"), their text is joined instead of overwritten.

    Args:
        text: The full resume text.

    Returns:
        Section name -> the text under it, in the order the sections appear.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None

    for line in text.splitlines():
        section = find_section_name(line)
        if section:
            current = section
            sections.setdefault(section, [])
        elif current and line.strip():
            sections[current].append(line.strip())

    return {name: "\n".join(lines) for name, lines in sections.items()}


def find_phone(text: str) -> str | None:
    """Return the first phone-like number with 10-15 digits, or None."""
    for match in PHONE_PATTERN.finditer(text):
        candidate = match.group().strip()
        digit_count = sum(char.isdigit() for char in candidate)
        if MIN_PHONE_DIGITS <= digit_count <= MAX_PHONE_DIGITS:
            return candidate
    return None


def first_match(pattern: re.Pattern[str], text: str) -> str | None:
    """Return the first regex match in the text, or None."""
    match = pattern.search(text)
    return match.group() if match else None


def extract_contact_info(text: str) -> ContactInfo:
    """Find the email, phone, LinkedIn and GitHub details in the text."""
    return ContactInfo(
        email=first_match(EMAIL_PATTERN, text),
        phone=find_phone(text),
        linkedin=first_match(LINKEDIN_PATTERN, text),
        github=first_match(GITHUB_PATTERN, text),
    )


# ---------- Main entry point ----------


def parse_resume(file_bytes: bytes, filename: str) -> ParsedResume:
    """Read a PDF or DOCX resume and pull out its text, sections and contact details.

    Takes raw bytes so it works with Streamlit uploads and files on disk alike.

    Args:
        file_bytes: The file contents.
        filename: The original file name (used for the extension).

    Returns:
        A ParsedResume.

    Raises:
        ResumeParseError: For unsupported, too large, damaged or empty/scanned files.
    """
    extension = check_file(filename, len(file_bytes))

    if extension == ".pdf":
        text = extract_pdf_text(file_bytes)
    else:
        text = extract_docx_text(file_bytes)

    if not text.strip():
        if extension == ".pdf":
            raise ResumeParseError(
                "No readable text was found in this PDF. It may be a scanned image. "
                "Please upload a text-based PDF or a DOCX file."
            )
        raise ResumeParseError("No text was found in this DOCX file.")

    return ParsedResume(
        text=text,
        sections=detect_sections(text),
        contact=extract_contact_info(text),
    )


def parse_resume_file(path: str | Path) -> ParsedResume:
    """Parse a resume stored on disk. See parse_resume() for details."""
    path = Path(path)
    return parse_resume(path.read_bytes(), path.name)


# Run from the command line:  python -m src.parser path/to/resume.pdf
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m src.parser path/to/resume.pdf")
        sys.exit(1)

    try:
        resume = parse_resume_file(sys.argv[1])
    except FileNotFoundError:
        print(f"File not found: {sys.argv[1]}")
        sys.exit(1)
    except ResumeParseError as error:
        print(f"Error: {error}")
        sys.exit(1)

    print("Sections found:", ", ".join(resume.sections) or "none")
    print("Contact details:")
    print(f"  Email:    {resume.contact.email or 'not found'}")
    print(f"  Phone:    {resume.contact.phone or 'not found'}")
    print(f"  LinkedIn: {resume.contact.linkedin or 'not found'}")
    print(f"  GitHub:   {resume.contact.github or 'not found'}")
    print("Word count:", resume.word_count)
