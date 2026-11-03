"""Tests for src/matcher.py, using hand-made Jobs (no API calls)."""

import pytest

from src.jobs.base import Job
from src.matcher import (
    COMMON_TECH_SKILLS,
    LOCATION_MISMATCH,
    LOCATION_PARTIAL,
    EXPERIENCE_FIT,
    EXPERIENCE_WEIGHT,
    LEVEL_BELOW,
    LEVEL_ONE_ABOVE,
    LEVEL_TWO_ABOVE,
    LOCATION_WEIGHT,
    NEUTRAL,
    SKILLS_WEIGHT,
    TITLE_WEIGHT,
    YEARS_MORE_THAN_2_SHORT,
    YEARS_UP_TO_1_SHORT,
    YEARS_UP_TO_2_SHORT,
    ExperienceFit,
    collect_user_skills,
    detect_required_years,
    detect_seniority,
    experience_part,
    extract_resume_skills,
    find_missing_skills,
    location_part,
    rank_jobs,
    remove_duplicates,
    skill_key,
    skills_part,
    skills_you_have,
    term_position,
    title_part,
    title_words,
)
from src.parser import ParsedResume
from src.profile import UserProfile


def make_job(title: str = "Machine Learning Engineer", **changes: object) -> Job:
    """Build a Job with test values, overriding any given fields."""
    values: dict = {
        "title": title,
        "company": "Demo AI",
        "location": "Lahore, Pakistan",
        "remote": False,
        "description": "",
        "url": "https://example.com/job",
        "source": "Test",
    }
    values.update(changes)
    return Job(**values)


def make_resume(skills_text: str = "") -> ParsedResume:
    """Build a resume whose Skills section has the given text."""
    sections = {"Skills": skills_text} if skills_text else {}
    return ParsedResume(text=skills_text, sections=sections)


ML_PROFILE = UserProfile(
    target_title="Machine Learning Engineer",
    experience_level="mid",
    years_experience=3,
    location="Lahore, Pakistan",
    work_type="onsite",
    skills={"Python": 5, "PyTorch": 4, "SQL": 3},
)


# --- The main promise: good matches rank higher ---


def test_well_matched_job_scores_higher_than_poor_one() -> None:
    good = make_job(
        "Machine Learning Engineer",
        description="Train models in Python and PyTorch, query data with SQL. 3+ years required.",
    )
    poor = make_job(
        "Accountant",
        company="Numbers Ltd",
        location="Berlin, Germany",
        description="Prepare tax returns and monthly ledgers in Excel.",
    )
    ranked = rank_jobs([poor, good], make_resume(), ML_PROFILE)

    assert [item.job.title for item in ranked] == ["Machine Learning Engineer", "Accountant"]
    assert ranked[0].score == 100
    # Only a neutral experience part (10) and a low location part (3) remain.
    assert ranked[1].score < 15


def test_scores_are_between_0_and_100() -> None:
    jobs = [make_job(f"Job {n}", description="Python " * n) for n in range(5)]
    for item in rank_jobs(jobs, make_resume("Python, Docker"), ML_PROFILE):
        assert 0 <= item.score <= 100


def test_without_profile_uses_resume_skills_and_neutral_parts() -> None:
    job = make_job(description="We use Python and Docker.")
    [item] = rank_jobs([job], make_resume("Python, Docker, Kubernetes"))
    assert item.matched_skills == ["Python", "Docker"]
    assert item.title_score == NEUTRAL
    assert item.location_score == NEUTRAL


# --- Skills ---


def test_skill_matching_is_whole_word_and_case_insensitive() -> None:
    assert term_position("We use PYTHON daily", "python") == 7
    assert term_position("Experience with JavaScript", "Java") is None
    assert term_position("C++ and C# developers", "C++") == 0
    assert term_position("Built with C#.", "C#") is not None
    assert term_position("Node.js, scikit-learn", "scikit-learn") is not None
    assert term_position("Node.js, scikit-learn", "Node.js") == 0
    assert term_position("NodeXjs", "Node.js") is None  # "." is not a wildcard


def test_skills_formula_uses_top_skill_weights() -> None:
    skills = {"Python": 5, "PyTorch": 4, "SQL": 3}  # target = 5 + 4 + 3 = 12
    score, matched = skills_part("Python and SQL", skills)
    assert score == pytest.approx(8 / 12)
    assert matched == ["Python", "SQL"]


def test_higher_rated_skills_count_more() -> None:
    skills = {"Python": 5, "SQL": 1}
    strong, _ = skills_part("Python", skills)
    weak, _ = skills_part("SQL", skills)
    assert strong > weak


def test_long_skill_list_is_not_punished() -> None:
    top_five = {"Python": 5, "PyTorch": 5, "SQL": 4, "Docker": 4, "AWS": 3}
    # The same top 5 plus 25 lower-rated skills the job doesn't mention.
    long_list = {**top_five, **{f"Tool{n}": 2 for n in range(25)}}
    job_text = "Python, SQL and AWS"
    assert skills_part(job_text, long_list)[0] == skills_part(job_text, top_five)[0]

    # Extra skills can only add matches, never lower the score.
    assert skills_part(job_text + " Tool3", long_list)[0] > skills_part(job_text, top_five)[0]


def test_matching_skills_worth_your_top_five_gives_full_points() -> None:
    many = {f"Skill{n}": 3 for n in range(12)}
    text = " ".join(f"Skill{n}" for n in range(5))
    assert skills_part(text, many)[0] == 1.0


def test_no_skills_gives_zero() -> None:
    assert skills_part("Python", {}) == (0.0, [])


def test_extract_resume_skills_handles_labels_and_separators() -> None:
    resume = make_resume(
        "Languages: Python, SQL; C++\n"
        "Tools | Docker | Git\n"
        "• scikit-learn · Node.js\n"
        "I have strong communication and leadership skills in teams"  # sentence: skipped
    )
    assert extract_resume_skills(resume) == [
        "Python", "SQL", "C++", "Tools", "Docker", "Git", "scikit-learn", "Node.js",
    ]


def test_profile_rating_wins_over_resume_and_duplicates_are_merged() -> None:
    profile = UserProfile(skills={"python": 5})
    skills = collect_user_skills(make_resume("Python, SQL, sql"), profile)
    assert skills == {"python": 5, "SQL": 3}


# --- Title ---


@pytest.mark.parametrize(
    ("job_title", "expected"),
    [
        ("Machine Learning Engineer", 1.0),
        ("Senior Machine Learning Engineer", 1.0),
        ("Lead ML Engineers - Vision", 1.0),
        ("Machine Learning Researcher", 2 / 2.2),  # both domain words, no "engineer"
        ("Software Engineer", 0.2 / 2.2),  # only the generic word
        ("Accountant", 0.0),
    ],
)
def test_title_similarity(job_title: str, expected: float) -> None:
    # Weights for "Machine Learning Engineer": machine 1.0 + learning 1.0 + engineer 0.2 = 2.2
    assert title_part(job_title, "Machine Learning Engineer") == pytest.approx(expected)


def test_generic_role_word_alone_gives_little_title_credit() -> None:
    score = title_part("Full-Stack Rails Engineer", "Machine Learning Engineer")
    assert score < 0.1
    assert TITLE_WEIGHT * score < 3  # under 3 of the 25 title points
    # Domain words carry the score: "Data Analyst" vs "Data Engineer" shares "data".
    assert title_part("Data Engineer", "Data Analyst") == pytest.approx(1 / 1.2)


def test_target_with_only_generic_words() -> None:
    assert title_part("Backend Engineer", "Engineer") == 1.0


def test_title_ignores_seniority_and_case() -> None:
    assert title_words("Sr. Junior DATA Analysts II") == {"data", "analyst"}


def test_empty_target_title_is_neutral() -> None:
    assert title_part("Anything", "") == NEUTRAL
    assert title_part("Anything", "Senior") == NEUTRAL  # only ignored words


# --- Location ---


def test_remote_job_for_remote_user_gets_full_points() -> None:
    profile = UserProfile(location="Lahore", work_type="remote")
    assert location_part(make_job(remote=True, location="Remote (Worldwide)"), profile) == 1.0


def test_job_in_your_city_gets_full_points() -> None:
    profile = UserProfile(location="Lahore, Pakistan", work_type="remote")
    assert location_part(make_job(location="Lahore, Punjab, Pakistan"), profile) == 1.0


def test_partial_points_for_same_country_or_remote() -> None:
    onsite = UserProfile(location="Lahore, Pakistan", work_type="onsite")
    assert location_part(make_job(location="Karachi, Pakistan"), onsite) == LOCATION_PARTIAL
    assert location_part(make_job(remote=True, location="Remote"), onsite) == LOCATION_PARTIAL


def test_other_location_gets_low_points() -> None:
    profile = UserProfile(location="Lahore", work_type="hybrid")
    assert location_part(make_job(location="London, UK"), profile) == LOCATION_MISMATCH


def test_no_location_preference_is_neutral() -> None:
    assert location_part(make_job(), None) == NEUTRAL
    assert location_part(make_job(), UserProfile(location="", work_type="onsite")) == NEUTRAL


# --- Missing skills ---


def test_missing_skills_in_order_of_appearance_max_5() -> None:
    job_text = "Docker, AWS, Kubernetes, Terraform, Airflow, Kafka and Python"
    missing = find_missing_skills(job_text, {"Python": 5, "aws": 3})
    assert missing == ["Docker", "Kubernetes", "Terraform", "Airflow", "Kafka"]


def test_missing_skills_shown_on_ranked_job() -> None:
    job = make_job(description="Python, PyTorch, Docker and MLflow required.")
    [item] = rank_jobs([job], make_resume(), ML_PROFILE)
    assert item.matched_skills == ["Python", "PyTorch"]
    # "Machine Learning" is in the job title but also in the target title, so it isn't "missing".
    assert item.missing_skills == ["Docker", "MLflow"]


def test_missing_skills_skip_words_in_target_title() -> None:
    job_text = "Machine Learning Engineer: Machine Learning, Docker"
    assert find_missing_skills(job_text, {}, "Machine Learning Engineer") == ["Docker"]
    assert find_missing_skills(job_text, {}) == ["Machine Learning", "Docker"]


def test_common_skill_list_size_and_no_duplicates() -> None:
    assert 90 <= len(COMMON_TECH_SKILLS) <= 130
    keys = [skill_key(skill) for skill in COMMON_TECH_SKILLS]
    assert len(keys) == len(set(keys))  # no "LLM" and "LLMs" pairs


# --- Fix 1: soft skills ---


def test_soft_skills_are_left_out_of_scoring_and_matches() -> None:
    resume = make_resume(
        "Python, Communication, Leadership, Team Collaboration, "
        "Problem Solving, Project Management, Technical Writing"
    )
    assert collect_user_skills(resume, None) == {"Python": 3}

    job = make_job(description="Python. Strong communication and leadership. Problem-solving.")
    [item] = rank_jobs([job], resume, ML_PROFILE)
    assert item.matched_skills == ["Python"]


def test_soft_skills_in_profile_are_ignored_too() -> None:
    profile = UserProfile(skills={"Communication": 5, "Leadership": 5, "SQL": 2})
    assert collect_user_skills(make_resume(), profile) == {"SQL": 2}


# --- Fix 3: single-letter skills ---


@pytest.mark.parametrize(
    ("text", "term", "found"),
    [
        ("Grade C or above, C-suite exposure", "C", False),
        ("Strong C programming skills", "C", True),
        ("Embedded C language and assembly", "C", True),
        ("Experience with C/C++", "C", True),
        ("R&D team, Plan R", "R", False),
        ("R programming for statistics", "R", True),
        ("Analysis in RStudio", "R", True),
        ("Python, Q, X", "Q", False),  # other single letters never match
    ],
)
def test_single_letter_skills_need_clear_forms(text: str, term: str, found: bool) -> None:
    assert (term_position(text, term) is not None) is found


def test_c_plus_plus_is_not_counted_as_c() -> None:
    assert term_position("C++ developer", "C") is None
    assert term_position("C/C++ developer", "C++") is not None


# --- Fix 4: missing skills check the whole resume ---


def test_skills_mentioned_anywhere_in_resume_are_not_missing() -> None:
    resume = ParsedResume(
        text="Experience\nBuilt Deep Learning models with Transformers and Docker.",
        sections={
            "Experience": "Built Deep Learning models with Transformers and Docker.",
            "Skills": "Python",
        },
    )
    job = make_job(description="Deep Learning, Transformers, Docker and Kubernetes required.")
    [item] = rank_jobs([job], resume, ML_PROFILE)
    assert item.missing_skills == ["Kubernetes"]


# --- Fix 5: singular and plural ---


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Experience with LLMs", "LLM"),
        ("Fine-tune an LLM", "LLMs"),
        ("Build REST APIs", "REST API"),
        ("Hugging Face Transformer models", "Transformers"),
    ],
)
def test_singular_and_plural_match(text: str, term: str) -> None:
    assert term_position(text, term) is not None


def test_plural_rule_does_not_create_false_matches() -> None:
    assert term_position("The team reacts quickly", "React") is None  # not an acronym
    assert term_position("Express.js", "Express") is not None  # "ss" isn't a plural
    assert term_position("AW", "AWS") is None  # short acronyms aren't treated as plural


def test_plural_skill_counts_as_matched_and_not_missing() -> None:
    profile = UserProfile(target_title="AI Engineer", skills={"LLMs": 5})
    job = make_job("AI Engineer", description="Deploy an LLM to production.")
    [item] = rank_jobs([job], make_resume(), profile)
    assert item.matched_skills == ["LLMs"]
    assert "LLM" not in item.missing_skills


def test_skill_key_treats_plural_and_case_the_same() -> None:
    assert skill_key("LLMs") == skill_key("llm") == "llm"
    assert skill_key("AWS") == "aws"
    assert skill_key("Express") == "express"


# --- Duplicates and sorting ---


def test_duplicates_keep_longest_description() -> None:
    short = make_job("Data Analyst", company="Acme", description="Short.", source="A")
    long = make_job("  data   ANALYST ", company="ACME ", description="A much longer description.", source="B")
    other = make_job("Data Analyst", company="Other Co", source="C")
    kept = remove_duplicates([short, other, long])
    assert [job.source for job in kept] == ["B", "C"]


def test_ranking_removes_duplicates() -> None:
    jobs = [make_job(source="A"), make_job(source="B", description="Longer text here")]
    ranked = rank_jobs(jobs, make_resume(), ML_PROFILE)
    assert len(ranked) == 1
    assert ranked[0].job.source == "B"


def test_equal_scores_keep_original_order() -> None:
    jobs = [make_job(company=f"Company {n}") for n in range(3)]
    ranked = rank_jobs(jobs, make_resume(), ML_PROFILE)
    assert [item.job.company for item in ranked] == ["Company 0", "Company 1", "Company 2"]


# --- Weights ---


def test_weights_add_up_to_100() -> None:
    assert (SKILLS_WEIGHT, TITLE_WEIGHT, EXPERIENCE_WEIGHT, LOCATION_WEIGHT) == (45, 20, 20, 15)


# --- Experience: seniority detection ---


@pytest.mark.parametrize(
    ("title", "description", "level"),
    [
        ("Machine Learning Intern", "", "junior"),
        ("Graduate Trainee - Data", "", "junior"),
        ("Jr. Data Analyst", "", "junior"),
        ("Entry-Level Python Developer", "", "junior"),
        ("Senior Data Scientist", "", "senior"),
        ("Sr Backend Engineer", "", "senior"),
        ("Lead ML Engineer", "", "senior"),
        ("Principal Engineer", "", "senior"),
        ("Staff Software Engineer", "", "senior"),
        ("Head of Data", "", "senior"),
        ("Mid-Level Data Engineer", "", "mid"),
        ("Data Analyst", "This is an entry-level role for new graduates.", "junior"),
        ("Data Analyst", "A senior-level position owning the roadmap.", "senior"),
        ("Data Analyst", "You will lead projects and work with senior engineers.", None),
        ("Data Analyst", "", None),
    ],
)
def test_detect_seniority(title: str, description: str, level: str | None) -> None:
    assert detect_seniority(make_job(title, description=description)) == level


def test_title_level_wins_over_description() -> None:
    job = make_job("Junior Data Analyst", description="Report to our senior-level manager.")
    assert detect_seniority(job) == "junior"


# --- Experience: required years ---


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("3+ years of Python", 3),
        ("You have 5 + yrs in ML", 5),
        ("at least 5 years in data science", 5),
        ("Minimum of 2 years experience", 2),
        ("2-4 years of experience", 2),  # range: the smallest number
        ("2 – 4 years experience", 2),
        ("1 to 3 years in a similar role", 1),
        ("3 years of experience with SQL", 3),
        ("over 2 years' professional experience", 2),
        ("three years experience required", 3),
        ("minimum of 1.5 years experience", 1.5),
        ("5+ years Python and 2+ years AWS", 5),  # several: the largest counts
    ],
)
def test_detect_required_years(text: str, years: float) -> None:
    assert detect_required_years(text) == years


@pytest.mark.parametrize(
    "text",
    [
        "Founded 30 years ago",
        "Over 10 years in business, serving clients worldwide",
        "A 2-year contract with option to extend",
        "Graduated 2019-2023",
        "No experience needed",
        "",
    ],
)
def test_text_without_required_years(text: str) -> None:
    assert detect_required_years(text) is None


# --- Experience: the score ---


def junior_profile(years: float = 1.3) -> UserProfile:
    """A junior user with the given years of experience."""
    return UserProfile(experience_level="junior", years_experience=years)


def years_job(years: str) -> Job:
    """A job with no level word in its title that asks for the given years."""
    return make_job("Data Analyst", description=f"{years} years of SQL experience")


def test_good_fit_gets_full_points() -> None:
    fit = experience_part(make_job("Junior Data Analyst"), junior_profile())
    assert fit == ExperienceFit(EXPERIENCE_FIT, "Junior role: good fit", False, True)


def test_enough_years_is_a_good_fit() -> None:
    fit = experience_part(years_job("1+"), junior_profile(1.3))
    assert fit.score == EXPERIENCE_FIT
    assert fit.note == "Asks for 1+ years, you have 1.3: good fit"
    assert fit.meets_requirement is True


@pytest.mark.parametrize(
    ("required", "user_years", "points", "verdict"),
    [
        ("2+", 2, EXPERIENCE_FIT, "good fit"),  # exactly enough
        ("2+", 1.5, YEARS_UP_TO_1_SHORT, "a bit above your level"),  # 0.5 short
        ("2+", 1, YEARS_UP_TO_1_SHORT, "a bit above your level"),  # exactly 1 short
        ("2+", 0.5, YEARS_UP_TO_2_SHORT, "a bit above your level"),  # 1.5 short
        ("2+", 0, YEARS_UP_TO_2_SHORT, "a bit above your level"),  # exactly 2 short
        ("3+", 0, YEARS_MORE_THAN_2_SHORT, "above your level"),  # 3 short
        ("5+", 1.3, YEARS_MORE_THAN_2_SHORT, "above your level"),
    ],
)
def test_years_scoring_steps(required: str, user_years: float, points: float, verdict: str) -> None:
    fit = experience_part(years_job(required), junior_profile(user_years))
    assert fit.score == points
    assert fit.note.endswith(verdict)
    assert fit.above_level is (verdict == "above your level")
    assert fit.meets_requirement is (points == EXPERIENCE_FIT)


def test_years_scores_are_strict() -> None:
    assert (YEARS_UP_TO_1_SHORT, YEARS_UP_TO_2_SHORT, YEARS_MORE_THAN_2_SHORT) == (0.5, 0.25, 0.05)


def test_senior_role_for_junior_is_above_level() -> None:
    fit = experience_part(make_job("Senior Data Analyst"), junior_profile())
    assert fit.score == LEVEL_TWO_ABOVE and fit.above_level is True


def test_one_level_above_is_partial() -> None:
    fit = experience_part(make_job("Mid-Level Analyst"), junior_profile())
    assert fit.score == LEVEL_ONE_ABOVE and fit.above_level is False


def test_job_below_your_level_gets_partial_not_zero() -> None:
    senior = UserProfile(experience_level="senior", years_experience=8)
    fit = experience_part(make_job("Data Analyst Intern"), senior)
    assert fit.score == LEVEL_BELOW > 0
    assert fit.note == "Junior role: below your level"
    assert fit.above_level is False
    assert fit.meets_requirement is False  # only a full 1.0 counts as meeting it


def test_toughest_requirement_decides() -> None:
    # The level fits, but the years don't.
    job = make_job("Junior Data Analyst", description="at least 5 years of experience")
    fit = experience_part(job, junior_profile(1))
    assert fit.score == YEARS_MORE_THAN_2_SHORT
    assert fit.note == "Junior role, asks for 5+ years, you have 1: above your level"
    assert fit.above_level is True


def test_student_matches_junior_roles() -> None:
    student = UserProfile(experience_level="student", years_experience=0)
    assert experience_part(make_job("Machine Learning Intern"), student).score == EXPERIENCE_FIT


def test_no_level_or_years_is_neutral_and_counts_as_meeting_it() -> None:
    fit = experience_part(make_job("Data Analyst"), junior_profile())
    assert fit == ExperienceFit(NEUTRAL, "No experience level given", False, True)


def test_no_profile_is_neutral() -> None:
    assert experience_part(make_job("Senior Data Analyst"), None).score == NEUTRAL


def test_experience_changes_ranking() -> None:
    fits = make_job("Junior ML Engineer", company="A", description="Python")
    too_senior = make_job("Senior ML Engineer", company="B", description="Python, 6+ years")
    profile = UserProfile(
        target_title="ML Engineer", experience_level="junior", years_experience=1,
        location="Lahore", work_type="onsite", skills={"Python": 5},
    )
    ranked = rank_jobs([too_senior, fits], make_resume(), profile)
    assert [item.job.company for item in ranked] == ["A", "B"]
    assert ranked[0].score - ranked[1].score == pytest.approx(
        EXPERIENCE_WEIGHT * (EXPERIENCE_FIT - YEARS_MORE_THAN_2_SHORT), abs=1
    )
    assert ranked[1].above_level is True
    assert ranked[1].meets_experience is False
    assert ranked[0].meets_experience is True
    assert ranked[1].experience_note.endswith("above your level")


def test_job_asking_2_years_ranks_below_job_with_no_requirement_for_0_years() -> None:
    # The reported problem: with 0 years, a "2+ years" job used to rank first.
    no_requirement = make_job("ML Engineer", company="A", description="Python and PyTorch")
    asks_two = make_job("ML Engineer", company="B", description="Python and PyTorch, 2+ years")
    profile = UserProfile(
        target_title="ML Engineer", experience_level="junior", years_experience=0,
        location="Lahore", work_type="onsite", skills={"Python": 5, "PyTorch": 4},
    )
    ranked = rank_jobs([asks_two, no_requirement], make_resume(), profile)
    assert [item.job.company for item in ranked] == ["A", "B"]
    assert ranked[1].experience_note == "Asks for 2+ years, you have 0: a bit above your level"


# --- Speed-ups must not change results ---


@pytest.mark.parametrize(
    ("text", "term", "found"),
    [
        ("Experience with PYTHON", "Python", True),  # case doesn't matter
        ("We use LLMs daily", "LLM", True),  # acronym plural
        ("Fine-tune an LLM", "LLMs", True),  # plural term, singular text
        ("Strong C programming", "C", True),  # single letter, clear form
        ("Grade C student", "C", False),
        ("JavaScript only", "Java", False),  # letters present, but not a whole word
        ("Nothing relevant here", "Kubernetes", False),  # quick check skips the regex
    ],
)
def test_term_position_quick_check_keeps_results(text: str, term: str, found: bool) -> None:
    assert (term_position(text, term) is not None) is found


def test_precomputed_skills_give_the_same_missing_skills() -> None:
    job_text = "Python, Docker, Kubernetes, Machine Learning, Deep Learning and AWS"
    user_skills = {"Python": 5}
    target = "Machine Learning Engineer"
    resume_text = "Built Deep Learning models"
    have = skills_you_have(user_skills, target, resume_text)

    assert {"Python", "Machine Learning", "Deep Learning"} <= have
    assert find_missing_skills(job_text, user_skills, target, resume_text) == (
        find_missing_skills(job_text, user_skills, have=have)
    ) == ["Docker", "Kubernetes", "AWS"]
