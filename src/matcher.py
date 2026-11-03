"""Scores and ranks jobs against the resume and user profile.

Each job gets a score from 0 to 100, made of four parts (weights from CLAUDE.md):

    score = 45 * skills + 20 * title + 20 * experience + 15 * location
    (each part is 0.0 - 1.0)
"""

import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache

from src.jobs.base import Job
from src.parser import ParsedResume
from src.profile import UserProfile

SKILLS_WEIGHT: int = 45
TITLE_WEIGHT: int = 20
EXPERIENCE_WEIGHT: int = 20
LOCATION_WEIGHT: int = 15

# --- Skills ---
# Skills found only in the resume's Skills section count like a 3/5 self-rating.
RESUME_SKILL_RATING: int = 3
# The skills part is full when the matched skills are worth as much as your
# top 5 skills, so skills further down a long list never lower your score.
TOP_SKILLS_FOR_SCORE: int = 5
MAX_MISSING_SKILLS: int = 5
# Lines longer than this in the Skills section are sentences, not skill names.
MAX_SKILL_WORDS: int = 4

# Soft skills almost every job mentions, so they say nothing about fit.
# They are left out of scoring and matched_skills. Compared with skill_key().
SOFT_SKILLS: set[str] = {
    "communication", "communication skill", "written communication", "verbal communication",
    "leadership", "team leadership", "teamwork", "team collaboration", "collaboration",
    "team player", "problem solving", "problem-solving", "critical thinking",
    "analytical thinking", "project management", "time management", "technical writing",
    "presentation", "presentation skill", "public speaking", "interpersonal skill",
    "adaptability", "creativity", "attention to detail", "mentoring", "self-motivated",
}

# Single-letter skills only count in these clear forms; a lone "C" or "R" is too
# common in normal text ("Grade C", "R&D"). Each is a regex, matched ignoring case.
SINGLE_LETTER_FORMS: dict[str, list[str]] = {
    "c": [r"(?<!\w)c programming(?!\w)", r"(?<!\w)c language(?!\w)", r"(?<!\w)c/c\+\+"],
    "r": [r"(?<!\w)r programming(?!\w)", r"(?<!\w)r language(?!\w)", r"(?<!\w)rstudio(?!\w)"],
}

# --- Title ---
# Seniority and filler words don't say what the job is, so they are ignored.
IGNORED_TITLE_WORDS: set[str] = {
    "senior", "sr", "junior", "jr", "lead", "principal", "staff", "head", "chief",
    "intern", "internship", "trainee", "associate", "entry", "level", "mid", "graduate",
    "i", "ii", "iii", "iv", "1", "2", "3",
    "and", "of", "the", "for", "with", "in", "at", "to", "a", "an",
}
# Short forms that mean the same as the full words.
TITLE_ABBREVIATIONS: dict[str, str] = {"ml": "machine learning", "swe": "software engineer"}
# Generic role words say little about the field, so they count much less than
# domain words like "machine", "learning" or "data".
GENERIC_ROLE_WORDS: set[str] = {
    "engineer", "developer", "analyst", "specialist", "programmer", "consultant",
    "architect", "manager", "technician", "officer", "expert", "professional",
}
GENERIC_WORD_WEIGHT: float = 0.2  # a domain word counts 1.0

# --- Location ---
LOCATION_PARTIAL: float = 0.5  # same country, or a remote job for someone who wanted on-site/hybrid
LOCATION_MISMATCH: float = 0.2  # somewhere else entirely
NEUTRAL: float = 0.5  # used when there's nothing to compare (no title, location or experience)

# --- Experience ---
# Levels as numbers so they can be compared. A student is matched like a junior.
LEVEL_RANKS: dict[str, int] = {"student": 1, "junior": 1, "mid": 2, "senior": 3}
# Seniority words looked for in the job title.
TITLE_LEVEL_WORDS: dict[str, list[str]] = {
    "junior": ["intern", "interns", "internship", "trainee", "graduate",
               "entry-level", "entry level", "junior", "jr"],
    "mid": ["mid-level", "mid level", "intermediate"],
    "senior": ["senior", "sr", "lead", "principal", "staff", "head"],
}
# In the description only clear phrases count: words like "lead" or "senior"
# appear in almost every description ("lead projects", "work with senior staff").
DESCRIPTION_LEVEL_PHRASES: dict[str, list[str]] = {
    "junior": ["entry-level", "entry level", "internship", "fresh graduate",
               "junior role", "junior position"],
    "mid": ["mid-level", "mid level"],
    "senior": ["senior-level", "senior level", "senior role", "senior position"],
}
EXPERIENCE_FIT: float = 1.0  # right level, or enough years
# Level (from the title or description):
LEVEL_ONE_ABOVE: float = 0.6  # e.g. a mid role for a junior
LEVEL_BELOW: float = 0.6  # below your level: you can do it, but it's not ideal
LEVEL_TWO_ABOVE: float = 0.2  # e.g. a senior role for a junior
# Required years, by how many years short you are:
YEARS_UP_TO_1_SHORT: float = 0.5
YEARS_UP_TO_2_SHORT: float = 0.25
YEARS_MORE_THAN_2_SHORT: float = 0.05  # counts as "above your level"
MAX_REQUIRED_YEARS: int = 15  # bigger numbers aren't experience requirements ("30 years in business")

# Regex parts for required years: digits like "3" or "1.5", or the words one to ten.
_NUMBER = r"(?<![\w.])(\d{1,2}(?:\.\d)?|one|two|three|four|five|six|seven|eight|nine|ten)"
_YEARS = r"(?:years?|yrs?)(?!\w)"
NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
# Checked in this order; each match is removed from the text before the next pattern,
# so "2-4 years of experience" isn't also read as "4 years of experience".
YEARS_PATTERNS: list[re.Pattern[str]] = [
    # "2-4 years", "2 to 4 years", "2–4 yrs": the smallest number is used
    re.compile(rf"{_NUMBER}\s*(?:-|–|—|to)\s*{_NUMBER}\s*\+?\s*{_YEARS}", re.IGNORECASE),
    # "at least 5 years", "minimum of 3 years"
    re.compile(rf"(?:at least|minimum(?: of)?|min\.?)\s+{_NUMBER}\s*\+?\s*{_YEARS}", re.IGNORECASE),
    # "3+ years"
    re.compile(rf"{_NUMBER}\s*\+\s*{_YEARS}", re.IGNORECASE),
    # "3 years of experience", "over 2 years' professional experience". This one needs
    # the word "experience", so "over 10 years in business" isn't read as a requirement.
    re.compile(rf"{_NUMBER}\s*{_YEARS}['’]?\s+(?:of\s+)?(?:[\w-]+\s+){{0,3}}?experience", re.IGNORECASE),
]

# Common tech skills, used to spot skills a job asks for that you haven't listed.
# Very short or everyday words ("R", "C", "Go", "Excel", "Spark") are left out
# because they match ordinary English too often. Plurals are matched automatically.
COMMON_TECH_SKILLS: list[str] = [
    # Languages
    "Python", "Java", "JavaScript", "TypeScript", "C++", "C#", "Golang", "Rust", "Kotlin",
    "Swift", "PHP", "Ruby", "Scala", "MATLAB", "SQL", "Bash", "HTML", "CSS",
    # Data and ML
    "Pandas", "NumPy", "scikit-learn", "TensorFlow", "PyTorch", "Keras", "XGBoost",
    "LightGBM", "Hugging Face", "Transformers", "LangChain", "LlamaIndex", "OpenCV",
    "NLP", "Computer Vision", "Deep Learning", "Machine Learning", "LLM", "RAG",
    "MLOps", "MLflow", "Kubeflow", "Jupyter", "Statistics", "A/B testing",
    "Data Visualization", "Matplotlib", "Seaborn", "Plotly", "Tableau", "Power BI",
    "Looker", "dbt", "Apache Spark", "PySpark", "Hadoop", "Kafka", "Airflow",
    "Databricks", "Snowflake", "BigQuery", "Redshift", "ETL",
    # Databases
    "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Elasticsearch", "Cassandra",
    "DynamoDB", "Pinecone", "FAISS",
    # Web and backend
    "React", "Angular", "Vue.js", "Next.js", "Node.js", "Express", "Django", "Flask",
    "FastAPI", "Spring Boot", ".NET", "GraphQL", "REST APIs", "gRPC", "Microservices",
    # Cloud and DevOps
    "AWS", "Azure", "GCP", "Google Cloud", "Docker", "Kubernetes", "Terraform",
    "Ansible", "Jenkins", "GitHub Actions", "CI/CD", "Linux", "Git", "SageMaker",
    "Vertex AI", "Lambda",
    # Practices and tools
    "Agile", "Scrum", "Jira", "Unit testing", "Pytest", "Selenium", "Figma",
]


@dataclass
class ScoredJob:
    """A job with its match score and skill details.

    Attributes:
        job: The original Job.
        score: Overall match from 0 to 100.
        matched_skills: Your skills that the job mentions, strongest first.
        missing_skills: Up to 5 common tech skills the job mentions that you haven't listed.
        experience_note: A short explanation, e.g. "Asks for 5+ years, you have 1.3: above your level".
        above_level: True if the job is clearly above your experience level.
        meets_experience: True if you meet the job's experience requirement (experience
            score 1.0) or the job doesn't state one.
        skills_score, title_score, experience_score, location_score: The four parts, each 0.0 - 1.0.
    """

    job: Job
    score: int
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    experience_note: str = ""
    above_level: bool = False
    meets_experience: bool = True
    skills_score: float = 0.0
    title_score: float = 0.0
    experience_score: float = 0.0
    location_score: float = 0.0


# ---------- Helpers ----------


def normalize(text: str) -> str:
    """Lowercase and collapse spaces, e.g. "  Data   Analyst " -> "data analyst"."""
    return " ".join(text.lower().split())


def is_plural(term: str) -> bool:
    """Return True for plural-looking terms like "LLMs" or "Transformers".

    Short words ("AWS") and words ending in "ss" ("Express") are not plurals.
    """
    return len(term) > 3 and term.lower().endswith("s") and not term.lower().endswith("ss")


def skill_key(name: str) -> str:
    """The form used to compare skills: lowercase and singular.

    "LLMs" and "llm" both give "llm"; "AWS" stays "aws".
    """
    key = normalize(name)
    return key[:-1] if is_plural(key) else key


def term_pattern(term: str) -> str:
    """Build the regex for a skill, matching it as a whole word in singular or plural.

    The term may not touch a letter or digit on either side, so "Java" doesn't
    match "JavaScript" while "C++", "Node.js" and "scikit-learn" still work.
    Plurals: "LLMs" also matches "LLM", and terms ending in an acronym
    ("LLM", "REST API") also match "LLMs" / "REST APIs". Other words ("React")
    are matched exactly, so "React" doesn't match "reacts".
    """
    term = term.strip()
    last_word = term.split()[-1] if term else ""
    if is_plural(term):
        body = re.escape(term[:-1]) + "s?"
    elif last_word.isalpha() and last_word.isupper() and len(last_word) > 1:
        body = re.escape(term) + "s?"
    else:
        body = re.escape(term)
    return rf"(?<!\w){body}(?!\w)"


@lru_cache(maxsize=1024)
def compiled_patterns(term: str) -> tuple[re.Pattern[str], ...]:
    """The compiled regex(es) for a term, built once and reused (ranking calls this a lot)."""
    if len(term.strip()) == 1:
        forms = SINGLE_LETTER_FORMS.get(term.strip().lower(), [])
    else:
        forms = [term_pattern(term)]
    return tuple(re.compile(form, re.IGNORECASE) for form in forms)


@lru_cache(maxsize=1024)
def quick_check_text(term: str) -> str:
    """Lowercase text that must appear in any match of `term` (empty for single letters).

    For "LLMs" this is "llm", because the pattern also matches the singular.
    """
    term = term.strip()
    if len(term) == 1:
        return ""
    return (term[:-1] if is_plural(term) else term).lower()


@lru_cache(maxsize=256)
def lowered(text: str) -> str:
    """text.lower(), remembered for the job and resume texts that are checked many times."""
    return text.lower()


def term_position(text: str, term: str) -> int | None:
    """Return where `term` first appears in `text`, or None. Case-insensitive.

    Single-letter skills ("C", "R") only count in clear forms such as
    "C programming", "C/C++" or "RStudio"; other single letters never match.
    """
    # Fast path: if the letters aren't in the text at all, no regex can match.
    # Most skills aren't in most job descriptions, so this skips most regex work.
    if quick_check_text(term) not in lowered(text):
        return None

    positions = [match.start() for pattern in compiled_patterns(term)
                 if (match := pattern.search(text))]
    return min(positions) if positions else None


def extract_resume_skills(resume: ParsedResume) -> list[str]:
    """Read skill names from the resume's Skills section.

    Handles lists like "SQL, Python | Docker" and labelled lines like
    "Languages: Python, SQL". Long phrases are skipped as they are sentences.
    """
    skills: list[str] = []
    for line in resume.sections.get("Skills", "").splitlines():
        if ":" in line:
            line = line.split(":", 1)[1]  # drop labels like "Languages:"
        for item in re.split(r"[,;|•·]", line):
            item = item.strip(" -*\t")
            if item and len(item.split()) <= MAX_SKILL_WORDS:
                skills.append(item)
    return skills


def collect_user_skills(resume: ParsedResume, profile: UserProfile | None) -> dict[str, int]:
    """Combine profile skills (with their 1-5 rating) and resume skills (rated 3).

    If a skill is in both, the profile rating wins. Duplicates are ignored
    regardless of case or plural ("LLM" / "LLMs"), keeping the first spelling seen.
    Soft skills like "Communication" are left out, as almost every job mentions them.

    Returns:
        Skill name -> weight, profile skills first.
    """
    skills: dict[str, int] = {}
    seen: set[str] = set()
    profile_skills = profile.skills if profile else {}
    resume_skills = {name: RESUME_SKILL_RATING for name in extract_resume_skills(resume)}

    for name, rating in list(profile_skills.items()) + list(resume_skills.items()):
        key = skill_key(name)
        if key and key not in seen and key not in SOFT_SKILLS:
            seen.add(key)
            skills[name.strip()] = rating
    return skills


# ---------- The three score parts ----------


def skills_part(job_text: str, user_skills: dict[str, int]) -> tuple[float, list[str]]:
    """Score how many of your skills the job mentions (0.0 - 1.0).

    Formula:
        matched = sum of the weights of your skills found in the job
        target  = sum of the weights of your top 5 skills
        score   = min(1, matched / target)

    The target only uses your top 5 skills, so adding more (lower-rated) skills
    to your list never lowers a score; they can only add matches. Matching
    skills worth as much as your top 5 already gives full points.

    Returns:
        (score, matched skill names, highest weight first)
    """
    if not user_skills:
        return 0.0, []

    matched = [name for name in user_skills if term_position(job_text, name) is not None]
    top_weights = sorted(user_skills.values(), reverse=True)[:TOP_SKILLS_FOR_SCORE]
    matched_weight = sum(user_skills[name] for name in matched)
    score = min(1.0, matched_weight / sum(top_weights))

    matched.sort(key=lambda name: user_skills[name], reverse=True)  # stable: ties keep order
    return score, matched


def title_words(title: str) -> set[str]:
    """Split a job title into meaningful words.

    Lowercases, expands short forms ("ML" -> "machine learning"), removes
    seniority/filler words and a plural "s", e.g.
    "Senior ML Engineers" -> {"machine", "learning", "engineer"}.
    """
    words: list[str] = []
    for word in re.findall(r"[a-z0-9+#]+", title.lower()):
        words += TITLE_ABBREVIATIONS.get(word, word).split()
    return {
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in words
        if word not in IGNORED_TITLE_WORDS
    }


def title_word_weight(word: str) -> float:
    """Domain words ("machine", "data") count 1.0; generic role words ("engineer") 0.2."""
    return GENERIC_WORD_WEIGHT if word in GENERIC_ROLE_WORDS else 1.0


def title_part(job_title: str, target_title: str) -> float:
    """Score how similar the job title is to your target title (0.0 - 1.0).

    Formula: the weighted share of your target title's words found in the job title.
    For "Machine Learning Engineer" the weights are machine 1.0 + learning 1.0
    + engineer 0.2 = 2.2, so:
        "Senior ML Engineer - Vision"  -> 2.2 / 2.2 = 1.0
        "Full-Stack Rails Engineer"    -> 0.2 / 2.2 = 0.09
    Extra words in the job title don't lower the score, as job titles often add them.
    """
    target = title_words(target_title)
    if not target:
        return NEUTRAL
    found = target & title_words(job_title)
    total = sum(title_word_weight(word) for word in target)
    return sum(title_word_weight(word) for word in found) / total


def location_part(job: Job, profile: UserProfile | None) -> float:
    """Score how well the job's location fits your preference (0.0 - 1.0).

    1.0 - remote job and you want remote, or the job is in your location
    0.5 - same country, or a remote job when you wanted on-site/hybrid
    0.2 - anything else
    """
    if profile is None or (not profile.location.strip() and profile.work_type != "remote"):
        return NEUTRAL

    if job.remote and profile.work_type == "remote":
        return 1.0

    job_location = normalize(job.location)
    parts = [normalize(part) for part in profile.location.split(",") if part.strip()]
    if parts and parts[0] in job_location:  # e.g. "lahore" in "lahore, pakistan"
        return 1.0
    if job.remote or (len(parts) > 1 and parts[-1] in job_location):  # same country
        return LOCATION_PARTIAL
    return LOCATION_MISMATCH


def skills_you_have(user_skills: dict[str, int], target_title: str, resume_text: str) -> set[str]:
    """Common tech skills that are never "missing" for you.

    That's skills you listed, skills mentioned anywhere in your resume, and
    skills in your target title. The answer is the same for every job, so
    rank_jobs() works it out once instead of once per job.
    """
    listed = {skill_key(name) for name in user_skills}
    return {
        skill
        for skill in COMMON_TECH_SKILLS
        if skill_key(skill) in listed
        or term_position(resume_text, skill) is not None
        or term_position(target_title, skill) is not None
    }


def find_missing_skills(
    job_text: str,
    user_skills: dict[str, int],
    target_title: str = "",
    resume_text: str = "",
    have: set[str] | None = None,
) -> list[str]:
    """List up to 5 common tech skills the job mentions that you don't have.

    A skill doesn't count as missing if it is in your skills, anywhere in your
    resume (e.g. "Deep Learning" in a job bullet), or in your target title
    ("Machine Learning" for a Machine Learning Engineer).
    Skills are returned in the order they first appear in the job text.

    `have` can pass in skills_you_have() worked out in advance, to save time.
    """
    if have is None:
        have = skills_you_have(user_skills, target_title, resume_text)
    found: list[tuple[int, str]] = []
    for skill in COMMON_TECH_SKILLS:
        if skill in have:
            continue
        position = term_position(job_text, skill)
        if position is not None:
            found.append((position, skill))
    found.sort()
    return [skill for _position, skill in found[:MAX_MISSING_SKILLS]]


def first_phrase(text: str, phrases_by_level: dict[str, list[str]]) -> str | None:
    """Return the level whose phrase appears earliest in the text, or None."""
    best: tuple[int, str] | None = None
    for level, phrases in phrases_by_level.items():
        for phrase in phrases:
            match = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE)
            if match and (best is None or match.start() < best[0]):
                best = (match.start(), level)
    return best[1] if best else None


def detect_seniority(job: Job) -> str | None:
    """Detect a job's level: "junior", "mid", "senior", or None if unclear.

    The title is checked first ("Senior Data Analyst" -> senior). If it has no
    level word, the description is checked for clear phrases like "entry-level".
    """
    return first_phrase(job.title, TITLE_LEVEL_WORDS) or first_phrase(
        job.description, DESCRIPTION_LEVEL_PHRASES
    )


def to_years(value: str) -> float:
    """Turn "3", "1.5" or "three" into a number."""
    return float(NUMBER_WORDS.get(value.lower(), value))


def detect_required_years(text: str) -> float | None:
    """Find how many years of experience a job asks for, or None.

    Understands "3+ years", "at least 5 years", "2-4 years of experience"
    (uses the smallest number of a range) and "three years' experience".
    If several are mentioned ("5+ years Python, 2+ years AWS"), the largest
    counts, as that is the job's main requirement.
    """
    found: list[float] = []
    for pattern in YEARS_PATTERNS:
        for match in pattern.finditer(text):
            numbers = [to_years(group) for group in match.groups() if group]
            found.append(min(numbers))  # a range gives two numbers: use the smaller
        text = pattern.sub(" ", text)  # don't read the same words again
    found = [years for years in found if 0 < years <= MAX_REQUIRED_YEARS]
    return max(found) if found else None


@dataclass
class ExperienceFit:
    """The result of comparing a job's experience requirement with your profile."""

    score: float  # 0.0 - 1.0
    note: str  # e.g. "Asks for 2+ years, you have 0: a bit above your level"
    above_level: bool  # clearly above you
    meets_requirement: bool  # score 1.0, or the job states no requirement


def level_result(level: str, profile: UserProfile) -> tuple[float, str]:
    """Compare the job's level with yours. Returns (points, kind)."""
    steps_above = LEVEL_RANKS[level] - LEVEL_RANKS.get(profile.experience_level, 1)
    if steps_above == 0:
        return EXPERIENCE_FIT, "fit"
    if steps_above < 0:
        return LEVEL_BELOW, "below"
    if steps_above == 1:
        return LEVEL_ONE_ABOVE, "stretch"
    return LEVEL_TWO_ABOVE, "above"


def years_result(required: float, profile: UserProfile) -> tuple[float, str]:
    """Compare the required years with yours. Returns (points, kind)."""
    years_short = required - profile.years_experience
    if years_short <= 0:
        return EXPERIENCE_FIT, "fit"
    if years_short <= 1:
        return YEARS_UP_TO_1_SHORT, "stretch"
    if years_short <= 2:
        return YEARS_UP_TO_2_SHORT, "stretch"
    return YEARS_MORE_THAN_2_SHORT, "above"


def experience_part(job: Job, profile: UserProfile | None) -> ExperienceFit:
    """Score how well the job's level and required years fit you (0.0 - 1.0).

    Level: same as yours = 1.0, one level above = 0.6, two or more above = 0.2,
           below yours = 0.6 (you can do it, but it's not ideal).
    Years: you have enough = 1.0, up to 1 year short = 0.5,
           1 to 2 years short = 0.25, more than 2 years short = 0.05.
    If the job gives both, the lower score counts. If it gives neither, 0.5.
    """
    if profile is None:
        return ExperienceFit(NEUTRAL, "Add your experience in tab 1 to compare", False, True)

    level = detect_seniority(job)
    required = detect_required_years(job.description)
    if level is None and required is None:
        return ExperienceFit(NEUTRAL, "No experience level given", False, True)

    # Each stated requirement gives (points, kind); kind is fit, below, stretch or above.
    results: list[tuple[float, str]] = []
    if level is not None:
        results.append(level_result(level, profile))
    if required is not None:
        results.append(years_result(required, profile))

    score = min(points for points, _kind in results)  # the toughest requirement decides
    kinds = [kind for _points, kind in results]
    if "above" in kinds:
        verdict = "above your level"
    elif "stretch" in kinds:
        verdict = "a bit above your level"
    elif "below" in kinds:
        verdict = "below your level"
    else:
        verdict = "good fit"

    parts: list[str] = []
    if level is not None:
        parts.append(f"{level.capitalize()} role")
    if required is not None:
        parts.append(f"asks for {required:g}+ years, you have {profile.years_experience:g}")
    note = ", ".join(parts)
    note = note[0].upper() + note[1:] + f": {verdict}"
    return ExperienceFit(score, note, "above" in kinds, score == EXPERIENCE_FIT)


# ---------- Ranking ----------


def remove_duplicates(jobs: list[Job]) -> list[Job]:
    """Drop jobs with the same title and company, keeping the longest description.

    The kept job stays where the first copy was in the list.
    """
    best: dict[tuple[str, str], Job] = {}
    for job in jobs:
        key = (normalize(job.title), normalize(job.company))
        if key not in best or len(job.description) > len(best[key].description):
            best[key] = job  # replacing a value keeps the dict's original order
    return list(best.values())


def score_job(
    job: Job,
    user_skills: dict[str, int],
    profile: UserProfile | None,
    resume_text: str = "",
    have: set[str] | None = None,
) -> ScoredJob:
    """Score one job against your skills and profile.

    `resume_text` is the whole resume, used so skills you mention anywhere
    in it are never listed as missing. `have` is skills_you_have(), if already known.
    """
    job_text = f"{job.title}\n{job.description}"
    skills_score, matched = skills_part(job_text, user_skills)
    target_title = profile.target_title if profile else ""
    if have is None:
        have = skills_you_have(user_skills, target_title, resume_text)
    title_score = title_part(job.title, target_title)
    experience = experience_part(job, profile)
    location_score = location_part(job, profile)

    total = (
        SKILLS_WEIGHT * skills_score
        + TITLE_WEIGHT * title_score
        + EXPERIENCE_WEIGHT * experience.score
        + LOCATION_WEIGHT * location_score
    )
    return ScoredJob(
        job=job,
        score=round(total),
        matched_skills=matched,
        missing_skills=find_missing_skills(job_text, user_skills, have=have),
        experience_note=experience.note,
        above_level=experience.above_level,
        meets_experience=experience.meets_requirement,
        skills_score=skills_score,
        title_score=title_score,
        experience_score=experience.score,
        location_score=location_score,
    )


def rank_jobs(
    jobs: list[Job], resume: ParsedResume, profile: UserProfile | None = None
) -> list[ScoredJob]:
    """Score every job, remove duplicates, and sort best first.

    Args:
        jobs: Jobs from any mix of sources.
        resume: The parsed resume (its Skills section is used).
        profile: The questionnaire answers: skills with ratings, target title,
            experience, location and work type. Without it, title, experience
            and location count as neutral.

    Returns:
        ScoredJobs sorted by score, highest first. Equal scores keep their order.
    """
    user_skills = collect_user_skills(resume, profile)
    # The same for every job, so worked out once here rather than once per job.
    have = skills_you_have(user_skills, profile.target_title if profile else "", resume.text)
    scored = [
        score_job(job, user_skills, profile, resume.text, have) for job in remove_duplicates(jobs)
    ]
    return sorted(scored, key=lambda item: item.score, reverse=True)


# Run from the command line, e.g.:
#   python -m src.matcher my_cv.pdf "Machine Learning Engineer" "Lahore" --level junior --years 1
# Searches JSearch and Remotive. Cached searches (6 hours) make no new API calls.
if __name__ == "__main__":
    import argparse

    from src.jobs.base import JobSourceError
    from src.jobs.jsearch import JSearchSource
    from src.jobs.remotive import RemotiveSource
    from src.parser import ResumeParseError, parse_resume_file
    from src.profile import EXPERIENCE_LEVELS

    arg_parser = argparse.ArgumentParser(
        prog="python -m src.matcher", description="Rank jobs from JSearch and Remotive against a resume."
    )
    arg_parser.add_argument("resume", help="path to a PDF or DOCX resume")
    arg_parser.add_argument("title", help='target job title, e.g. "Machine Learning Engineer"')
    arg_parser.add_argument("location", help='e.g. "Lahore"')
    arg_parser.add_argument("--remote", action="store_true", help="search remote jobs only")
    arg_parser.add_argument(
        "--level", choices=EXPERIENCE_LEVELS, default="junior", help="your experience level (default: junior)"
    )
    arg_parser.add_argument(
        "--years", type=float, default=0, help="your years of experience (default: 0)"
    )
    args = arg_parser.parse_args()
    if args.years < 0:
        arg_parser.error("--years cannot be negative")

    try:
        parsed = parse_resume_file(args.resume)
    except FileNotFoundError:
        print(f"File not found: {args.resume}")
        sys.exit(1)
    except ResumeParseError as error:
        print(f"Error: {error}")
        sys.exit(1)

    cli_profile = UserProfile(
        target_title=args.title,
        experience_level=args.level,
        years_experience=args.years,
        location=args.location,
        work_type="remote" if args.remote else "onsite",
    )
    print(f"Comparing against: {args.level}, {args.years:g} year(s) of experience")

    all_jobs: list[Job] = []
    for source in [JSearchSource(), RemotiveSource()]:
        try:
            found = source.search(args.title, args.location, args.remote)
            print(f"{source.name}: {len(found)} job(s)")
            all_jobs += found
        except JobSourceError as error:
            print(f"Warning: {error}")

    ranked = rank_jobs(all_jobs, parsed, cli_profile)
    print(f"\nTop {min(10, len(ranked))} of {len(ranked)} job(s) after removing duplicates:\n")
    for number, item in enumerate(ranked[:10], start=1):
        print(f"{number}. [{item.score}] {item.job.title} at {item.job.company}")
        print(f"   Source:     {item.job.source}")
        print(f"   Matched:    {', '.join(item.matched_skills) or 'none'}")
        print(f"   Missing:    {', '.join(item.missing_skills) or 'none'}")
        print(f"   Experience: {item.experience_note}")
