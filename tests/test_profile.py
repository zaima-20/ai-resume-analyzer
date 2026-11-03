"""Tests for src/profile.py."""

from src.profile import UserProfile


def make_valid_profile() -> UserProfile:
    """Build a profile that should pass validation."""
    return UserProfile(
        target_title="Data Analyst",
        experience_level="junior",
        years_experience=1.5,
        skills={"SQL": 4, "Python": 5, "Excel": 3, "Tableau": 4},
        location="Lahore",
        work_type="hybrid",
        job_sources=["JSearch", "Remotive"],
    )


# --- validate() ---


def test_valid_profile_has_no_errors() -> None:
    assert make_valid_profile().validate() == []


def test_empty_title_is_an_error() -> None:
    profile = make_valid_profile()
    profile.target_title = "   "
    errors = profile.validate()
    assert len(errors) == 1
    assert "title" in errors[0].lower()


def test_negative_years_is_an_error() -> None:
    profile = make_valid_profile()
    profile.years_experience = -1
    assert any("negative" in e for e in profile.validate())


def test_rating_outside_range_is_an_error() -> None:
    profile = make_valid_profile()
    profile.skills = {"SQL": 0, "Python": 6, "Excel": 3}
    errors = profile.validate()
    assert len(errors) == 2
    assert any("SQL" in e for e in errors)
    assert any("Python" in e for e in errors)


def test_boundary_ratings_are_valid() -> None:
    profile = make_valid_profile()
    profile.skills = {"SQL": 1, "Python": 5}
    assert profile.validate() == []


def test_empty_skill_name_is_an_error() -> None:
    profile = make_valid_profile()
    profile.skills = {"": 3}
    assert any("Skill names" in e for e in profile.validate())


def test_unknown_experience_level_is_an_error() -> None:
    profile = make_valid_profile()
    profile.experience_level = "expert"
    assert any("Experience level" in e for e in profile.validate())


def test_unknown_work_type_is_an_error() -> None:
    profile = make_valid_profile()
    profile.work_type = "anywhere"
    assert any("Work type" in e for e in profile.validate())


def test_multiple_problems_are_all_reported() -> None:
    profile = UserProfile(target_title="", years_experience=-2, skills={"Go": 9})
    assert len(profile.validate()) == 3


# --- top_skills() ---


def test_top_skills_returns_highest_rated_first() -> None:
    assert make_valid_profile().top_skills(2) == ["Python", "SQL"]


def test_top_skills_keeps_entry_order_for_ties() -> None:
    # SQL and Tableau are both rated 4; SQL was entered first.
    assert make_valid_profile().top_skills(3) == ["Python", "SQL", "Tableau"]


def test_top_skills_n_larger_than_skill_count() -> None:
    assert len(make_valid_profile().top_skills(10)) == 4


def test_top_skills_zero_or_negative_n() -> None:
    profile = make_valid_profile()
    assert profile.top_skills(0) == []
    assert profile.top_skills(-1) == []


def test_top_skills_with_no_skills() -> None:
    assert UserProfile().top_skills(3) == []


def test_default_lists_are_not_shared() -> None:
    a, b = UserProfile(), UserProfile()
    a.job_sources.append("Adzuna")
    a.skills["SQL"] = 3
    assert b.job_sources == []
    assert b.skills == {}
