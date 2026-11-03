"""UserProfile dataclass for the skills questionnaire."""

from dataclasses import dataclass, field

# Allowed choices, shared with the UI so the dropdowns and validation agree.
EXPERIENCE_LEVELS: list[str] = ["student", "junior", "mid", "senior"]
WORK_TYPES: list[str] = ["remote", "onsite", "hybrid"]
# Job sources the user can pick from. These match the modules in src/jobs/.
JOB_SOURCES: list[str] = ["JSearch", "Adzuna", "Remotive", "Search links"]
MIN_RATING: int = 1
MAX_RATING: int = 5


@dataclass
class UserProfile:
    """Answers from the skills questionnaire.

    Attributes:
        target_title: Job title the user is aiming for, e.g. "Data Analyst".
        experience_level: One of EXPERIENCE_LEVELS.
        years_experience: Years of work experience (0 or more, decimals allowed).
        skills: Skill name -> self-rating from 1 (beginner) to 5 (expert).
        location: Preferred city or country, e.g. "Lahore" (may be empty).
        work_type: One of WORK_TYPES.
        job_sources: Names of the job sources the user wants to search.
    """

    target_title: str = ""
    experience_level: str = "junior"
    years_experience: float = 0
    skills: dict[str, int] = field(default_factory=dict)
    location: str = ""
    work_type: str = "remote"
    job_sources: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Check the profile and return a list of problems.

        Returns:
            Human-readable error messages. An empty list means the profile is valid.
        """
        errors: list[str] = []

        if not self.target_title.strip():
            errors.append("Target job title cannot be empty.")

        if self.experience_level not in EXPERIENCE_LEVELS:
            errors.append(
                f"Experience level must be one of: {', '.join(EXPERIENCE_LEVELS)}."
            )

        if self.years_experience < 0:
            errors.append("Years of experience cannot be negative.")

        for name, rating in self.skills.items():
            if not name.strip():
                errors.append("Skill names cannot be empty.")
            elif not MIN_RATING <= rating <= MAX_RATING:
                errors.append(
                    f"Rating for '{name}' must be between {MIN_RATING} and {MAX_RATING}."
                )

        if self.work_type not in WORK_TYPES:
            errors.append(f"Work type must be one of: {', '.join(WORK_TYPES)}.")

        return errors

    def top_skills(self, n: int = 5) -> list[str]:
        """Return the names of the n highest-rated skills.

        Skills with the same rating keep the order the user entered them in.

        Args:
            n: How many skill names to return. 0 or less returns an empty list.

        Returns:
            Skill names, highest rating first.
        """
        if n <= 0:
            return []
        # sorted() is stable, so ties stay in their original order.
        ranked = sorted(self.skills.items(), key=lambda item: item[1], reverse=True)
        return [name for name, _rating in ranked[:n]]
