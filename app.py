"""Streamlit UI for the AI Resume Analyzer (UI only, no business logic)."""

import copy
from datetime import date, datetime, timezone

import streamlit as st

from src.auth import AuthError, AuthUser, current_user, sign_in, sign_out, sign_up
from src.config import DAILY_AI_REVIEW_LIMIT, DAILY_SEARCH_LIMIT
from src.db import (
    AI_REVIEWS,
    SEARCHES,
    DatabaseError,
    SavedAnalysis,
    SavedJob,
    delete_my_account,
    delete_my_data,
    list_analyses,
    list_saved_jobs,
    load_profile,
    remove_saved_job,
    save_analysis,
    save_job,
    save_profile,
    usage_today,
    use_quota,
)
from src.jobs.adzuna import AdzunaSource, find_country
from src.jobs.base import Job, JobSource, JobSourceError
from src.jobs.jsearch import JSearchSource
from src.jobs.remotive import RemotiveSource
from src.jobs.search_links import build_search_links
from src.llm_review import LLMReview, LLMReviewError, review_resume
from src.matcher import ScoredJob, rank_jobs
from src.parser import ParsedResume, ResumeParseError, parse_resume
from src.rule_checks import HIGH, LOW, MEDIUM, Issue, run_checks
from src.timing import timed
from src.profile import (
    EXPERIENCE_LEVELS,
    JOB_SOURCES,
    MAX_RATING,
    MIN_RATING,
    WORK_TYPES,
    UserProfile,
)

DEFAULT_RATING: int = 3

# Profile job source names -> the class that searches it. "Search links" isn't
# an API; it's the links section at the bottom of the Jobs tab.
JOB_SOURCE_CLASSES: dict[str, type[JobSource]] = {
    "JSearch": JSearchSource,
    "Adzuna": AdzunaSource,
    "Remotive": RemotiveSource,
}


# ---------- Session state setup ----------


def init_session_state() -> None:
    """Create the session_state entries this page needs, only on the first run."""
    if "skill_names" not in st.session_state:
        st.session_state.skill_names = []  # skills in the order the user added them
    if "profile" not in st.session_state:
        st.session_state.profile = None  # the saved UserProfile, once valid
    if "skill_message" not in st.session_state:
        st.session_state.skill_message = ""  # warning from the last "Add skill" click
    if "resume" not in st.session_state:
        st.session_state.resume = None  # the ParsedResume, once a file parses
    if "resume_file_id" not in st.session_state:
        st.session_state.resume_file_id = None  # which upload `resume` came from
    if "resume_error" not in st.session_state:
        st.session_state.resume_error = ""  # parse error for that upload, if any
    if "ai_review" not in st.session_state:
        st.session_state.ai_review = None  # the last LLMReview from Gemini
    if "ai_review_error" not in st.session_state:
        st.session_state.ai_review_error = ""  # error from the last AI review, if any
    if "ai_review_file_id" not in st.session_state:
        st.session_state.ai_review_file_id = None  # which upload the review belongs to
    if "job_results" not in st.session_state:
        st.session_state.job_results = None  # ranked ScoredJobs from the last search
    if "job_messages" not in st.session_state:
        st.session_state.job_messages = []  # (kind, text) notes from the last search
    if "job_search_inputs" not in st.session_state:
        st.session_state.job_search_inputs = None  # what the last search was based on
    if "job_search_count" not in st.session_state:
        st.session_state.job_search_count = 0  # bumped each search, to reset the filters
    if "resume_file_name" not in st.session_state:
        st.session_state.resume_file_name = None  # name of the uploaded file, for saving
    if "profile_loaded" not in st.session_state:
        st.session_state.profile_loaded = False  # saved profile already loaded this login?
    if "db_warning" not in st.session_state:
        st.session_state.db_warning = ""  # problem loading the saved profile, if any
    if "ai_review_save_note" not in st.session_state:
        st.session_state.ai_review_save_note = None  # (kind, text) after saving an analysis
    if "past_analyses" not in st.session_state:
        st.session_state.past_analyses = None  # loaded once, reloaded after each save
    if "past_analyses_error" not in st.session_state:
        st.session_state.past_analyses_error = ""
    if "saved_jobs" not in st.session_state:
        st.session_state.saved_jobs = None  # loaded once per login, reloaded after save/remove
    if "saved_jobs_error" not in st.session_state:
        st.session_state.saved_jobs_error = ""
    if "usage" not in st.session_state:
        st.session_state.usage = None  # today's counts, loaded once and updated locally
    if "usage_day" not in st.session_state:
        st.session_state.usage_day = None  # the UTC day `usage` belongs to
    if "checks_memo" not in st.session_state:
        st.session_state.checks_memo = None  # (inputs, issues) from the last run_checks()
    if "confirm_delete" not in st.session_state:
        st.session_state.confirm_delete = None  # "data" or "account" while asking "Are you sure?"
    if "account_message" not in st.session_state:
        st.session_state.account_message = None  # (kind, text) after a delete

    # Starting values for the tab 1 form. They live here (not as widget defaults)
    # so a saved profile can fill the form without Streamlit warnings.
    form_defaults = {
        "target_title": "",
        "experience_level": "junior",
        "years_experience": 0.0,
        "location": "",
        "work_type": WORK_TYPES[0],
        "job_sources": list(JOB_SOURCES),
    }
    for key, value in form_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

# ---------- Callbacks for the skill buttons ----------
# Callbacks run before the page redraws, so the new skill list shows up immediately.


def add_skill() -> None:
    """Add the skill typed in the text box, ignoring blanks and duplicates."""
    name = st.session_state.new_skill.strip()
    existing = [s.lower() for s in st.session_state.skill_names]

    if not name:
        st.session_state.skill_message = "Type a skill name first."
    elif name.lower() in existing:
        st.session_state.skill_message = f"'{name}' is already in your list."
    else:
        st.session_state.skill_names.append(name)
        st.session_state[f"rating_{name}"] = DEFAULT_RATING
        st.session_state.skill_message = ""

    st.session_state.new_skill = ""  # clear the text box


def remove_skill(name: str) -> None:
    """Remove a skill and forget its slider value."""
    st.session_state.skill_names.remove(name)
    st.session_state.pop(f"rating_{name}", None)


# ---------- Daily limits ----------

# kind -> (daily limit, singular name, plural name)
LIMITED_ACTIONS: dict[str, tuple[int, str, str]] = {
    SEARCHES: (DAILY_SEARCH_LIMIT, "job search", "job searches"),
    AI_REVIEWS: (DAILY_AI_REVIEW_LIMIT, "AI review", "AI reviews"),
}


def utc_today() -> date:
    """Today's date in UTC, the day the database counts usage by."""
    return datetime.now(timezone.utc).date()


def uses_left(kind: str) -> int | None:
    """How many uses of a limited action are left today, or None if unknown.

    The counts are loaded once per login (and again when the UTC day changes),
    then kept up to date locally by try_use(), not reloaded on every click.
    """
    if st.session_state.usage is None or st.session_state.usage_day != utc_today():
        st.session_state.usage_day = utc_today()
        try:
            st.session_state.usage = usage_today()
        except DatabaseError:
            st.session_state.usage = {}  # unknown; try_use() still checks on click
    used = st.session_state.usage.get(kind)
    if used is None:
        return None
    return max(0, LIMITED_ACTIONS[kind][0] - used)


def show_uses_left(kind: str) -> None:
    """Show e.g. "2 of 3 AI reviews left today" under a button."""
    limit, _singular, plural = LIMITED_ACTIONS[kind]
    left = uses_left(kind)
    if left is None:
        st.caption(f"Limit: {limit} {plural} per day.")
    elif left == 0:
        st.caption(f"You've used your {limit} {plural} for today. Try again tomorrow.")
    else:
        st.caption(f"{left} of {limit} {plural} left today.")


def try_use(kind: str) -> bool:
    """Count one use before a limited action. Returns False (with a message) if it must not run.

    The database does the counting (increment_usage), so users can't reset it.
    If the check itself fails, the action is blocked too, to protect the API quotas.
    """
    limit, singular, plural = LIMITED_ACTIONS[kind]
    try:
        allowed = use_quota(kind, limit)
    except DatabaseError as error:
        st.warning(
            f"Couldn't check your daily limit, so the {singular} wasn't run "
            f"(this protects the free API quotas). {error}"
        )
        return False
    # The database's answer tells us the new count, so update it here without reloading.
    usage = st.session_state.usage
    if usage and kind in usage:
        usage[kind] = usage[kind] + 1 if allowed else max(usage[kind], limit)
    if not allowed:
        st.info(f"You've used your {limit} {plural} for today. Try again tomorrow.")
    return allowed


# ---------- Tab 1: profile ----------


def fill_profile_form(profile: UserProfile) -> None:
    """Put a saved profile into the tab 1 form. Values the form can't show are skipped."""
    st.session_state.target_title = profile.target_title
    if profile.experience_level in EXPERIENCE_LEVELS:
        st.session_state.experience_level = profile.experience_level
    st.session_state.years_experience = max(0.0, float(profile.years_experience))
    st.session_state.location = profile.location
    if profile.work_type in WORK_TYPES:
        st.session_state.work_type = profile.work_type
    st.session_state.job_sources = [s for s in profile.job_sources if s in JOB_SOURCES]

    st.session_state.skill_names = list(profile.skills)
    for name, rating in profile.skills.items():
        st.session_state[f"rating_{name}"] = min(MAX_RATING, max(MIN_RATING, int(rating)))

    if not profile.validate():
        st.session_state.profile = profile


def load_saved_profile() -> None:
    """Fill tab 1 with the user's saved profile, once per login."""
    if st.session_state.profile_loaded:
        return
    st.session_state.profile_loaded = True  # don't ask the database again on every click
    try:
        profile = load_profile()
    except DatabaseError as error:
        st.session_state.db_warning = str(error)
        return
    if profile is not None:
        fill_profile_form(profile)


def render_profile_tab() -> None:
    """Draw the questionnaire, then validate and save the profile on click."""
    st.header("Your Profile")
    st.write("Tell us what you're aiming for. We use this to review your resume and find jobs.")
    if st.session_state.db_warning:
        st.warning(f"Your saved profile couldn't be loaded. {st.session_state.db_warning}")

    st.text_input("Target job title", key="target_title", placeholder="e.g. Data Analyst")

    col1, col2 = st.columns(2)
    with col1:
        st.selectbox(
            "Experience level",
            EXPERIENCE_LEVELS,
            format_func=str.capitalize,
            key="experience_level",
        )
    with col2:
        st.number_input(
            "Years of experience", min_value=0.0, step=0.5, key="years_experience"
        )

    col3, col4 = st.columns(2)
    with col3:
        st.text_input("Preferred location", key="location", placeholder="e.g. Lahore")
    with col4:
        st.radio(
            "Work type",
            WORK_TYPES,
            format_func=str.capitalize,
            horizontal=True,
            key="work_type",
        )

    st.multiselect("Job sources to search", JOB_SOURCES, key="job_sources")

    # --- Skills: add one at a time, rate each, remove any ---
    st.subheader("Your top skills")
    st.caption(f"Rate yourself from {MIN_RATING} (beginner) to {MAX_RATING} (expert).")

    add_col, button_col = st.columns([4, 1], vertical_alignment="bottom")
    with add_col:
        st.text_input("Add a skill", key="new_skill", placeholder="e.g. Python")
    with button_col:
        st.button("Add skill", on_click=add_skill, width="stretch")

    if st.session_state.skill_message:
        st.warning(st.session_state.skill_message)

    if not st.session_state.skill_names:
        st.info("No skills added yet.")

    for name in st.session_state.skill_names:
        slider_col, remove_col = st.columns([4, 1], vertical_alignment="center")
        with slider_col:
            # The starting value is set in add_skill() or when a saved profile is loaded.
            st.slider(name, min_value=MIN_RATING, max_value=MAX_RATING, key=f"rating_{name}")
        with remove_col:
            st.button(
                "Remove",
                key=f"remove_{name}",
                on_click=remove_skill,
                args=(name,),
                width="stretch",
            )

    st.divider()

    if st.button("Save profile", type="primary"):
        # Collect the widget values (Streamlit keeps them in session_state by key).
        profile = UserProfile(
            target_title=st.session_state.target_title,
            experience_level=st.session_state.experience_level,
            years_experience=st.session_state.years_experience,
            skills={
                name: st.session_state[f"rating_{name}"]
                for name in st.session_state.skill_names
            },
            location=st.session_state.location.strip(),
            work_type=st.session_state.work_type,
            job_sources=list(st.session_state.job_sources),
        )

        errors = profile.validate()
        if errors:
            for error in errors:
                st.error(error)
        else:
            st.session_state.profile = profile
            try:
                save_profile(profile)
                st.session_state.db_warning = ""
                st.success("Profile saved! Saved to your account. Next, upload your resume in tab 2.")
            except DatabaseError as error:
                # The app keeps working with the profile for this visit.
                st.success("Profile saved for this visit. Next, upload your resume in tab 2.")
                st.warning(f"Not saved to your account. {error}")


# ---------- Tab 2: resume upload ----------


def show_contact_line(label: str, value: str | None) -> None:
    """Show one contact detail, or an orange "Not found" if it's missing."""
    if value:
        st.markdown(f"**{label}:** {value}")
    else:
        st.markdown(f"**{label}:** :orange[Not found]")


def show_parsed_resume(resume: ParsedResume) -> None:
    """Display the word count, contact details and sections of a parsed resume."""
    st.metric("Word count", resume.word_count)

    st.subheader("Contact details")
    contact_left, contact_right = st.columns(2)
    with contact_left:
        show_contact_line("Email", resume.contact.email)
        show_contact_line("Phone", resume.contact.phone)
    with contact_right:
        show_contact_line("LinkedIn", resume.contact.linkedin)
        show_contact_line("GitHub", resume.contact.github)

    st.subheader("Sections found")
    if not resume.sections:
        st.warning("No section headings were found, such as Experience, Education or Skills.")
        return
    st.write(", ".join(resume.sections))

    for name, text in resume.sections.items():
        with st.expander(name):
            st.text(text or "(empty)")


def render_upload_tab() -> None:
    """Let the user upload a resume, parse it once per file, and show the result."""
    st.header("Upload Resume")

    if st.session_state.profile is None:
        st.info("Tip: fill in and save tab 1 first, so the analysis can compare your resume to your goals.")

    uploaded = st.file_uploader("Upload your resume (PDF or DOCX)", type=["pdf", "docx"])

    if uploaded is None:
        # The file was removed (or never added), so forget the old result.
        st.session_state.resume = None
        st.session_state.resume_file_id = None
        st.session_state.resume_file_name = None
        st.session_state.resume_error = ""
        return

    # Streamlit reruns this script on every click, so only parse when the upload changes.
    # file_id is new for every upload, even if the same file is uploaded again.
    if uploaded.file_id != st.session_state.resume_file_id:
        st.session_state.resume_file_id = uploaded.file_id
        st.session_state.resume_file_name = uploaded.name
        try:
            with timed("parse_resume"):
                st.session_state.resume = parse_resume(uploaded.getvalue(), uploaded.name)
            st.session_state.resume_error = ""
        except ResumeParseError as error:
            st.session_state.resume = None
            st.session_state.resume_error = str(error)

    if st.session_state.resume_error:
        st.error(st.session_state.resume_error)
        return

    st.success(f"Read {uploaded.name} successfully.")
    show_parsed_resume(st.session_state.resume)


# ---------- Tab 3: analysis ----------

# Heading and box function for each severity: st.error is red, st.warning orange, st.info blue.
SEVERITY_DISPLAY = [
    (HIGH, "High", st.error),
    (MEDIUM, "Medium", st.warning),
    (LOW, "Low", st.info),
]


def show_issue(issue: Issue, box) -> None:
    """Show one issue inside a coloured box (st.error, st.warning or st.info)."""
    box(
        f"**{issue.issue}**  \n"  # two spaces before \n make a line break in Markdown
        f"**Where:** {issue.location}  \n"
        f"**Fix:** {issue.suggested_fix}"
    )


def render_analysis_tab() -> None:
    """Run the rule checks on the uploaded resume and show the issues by severity."""
    st.header("Analysis")

    resume = st.session_state.resume
    if resume is None:
        st.info("Upload your resume in tab 2 to see the analysis.")
        show_past_analyses()
        return  # end this tab only; st.stop() would also hide the tabs after it

    profile = st.session_state.profile
    if profile is None:
        st.caption("Save your profile in tab 1 to also check the skills you listed.")

    issues = checks_for(resume, profile)

    st.subheader("Quick checks")
    metric_columns = st.columns(len(SEVERITY_DISPLAY))
    for column, (severity, label, _box) in zip(metric_columns, SEVERITY_DISPLAY):
        count = sum(1 for issue in issues if issue.severity == severity)
        column.metric(f"{label} issues", count)

    if not issues:
        st.success("No problems found by the quick checks. Nice work!")
    else:
        for severity, label, box in SEVERITY_DISPLAY:
            group = [issue for issue in issues if issue.severity == severity]
            if not group:
                continue
            st.markdown(f"#### {label}")
            for issue in group:
                show_issue(issue, box)

    st.divider()
    render_ai_review(resume, profile, issues)
    show_past_analyses()


def show_past_analyses() -> None:
    """Show the user's saved analyses in a collapsible section, newest first."""
    # Loaded once, and again after each save, not on every click.
    if st.session_state.past_analyses is None:
        try:
            st.session_state.past_analyses = list_analyses()
            st.session_state.past_analyses_error = ""
        except DatabaseError as error:
            st.session_state.past_analyses = []
            st.session_state.past_analyses_error = str(error)

    analyses: list[SavedAnalysis] = st.session_state.past_analyses
    with st.expander(f"Past analyses ({len(analyses)})"):
        if st.session_state.past_analyses_error:
            st.warning(f"Couldn't load your past analyses. {st.session_state.past_analyses_error}")
        elif not analyses:
            st.caption("Nothing saved yet. Each AI review is saved here automatically.")
        else:
            st.dataframe(
                [
                    {
                        "Date": item.created_at.strftime("%d %b %Y, %H:%M UTC"),
                        "File": item.file_name,
                        "Overall": item.overall_score,
                        "ATS": item.ats_score,
                        "Issues": len(item.issues),
                        "Words": item.word_count,
                    }
                    for item in analyses
                ],
                hide_index=True,
            )


def escape_markdown(text: str) -> str:
    """Escape "$" so Streamlit doesn't render "[$X] ... $Y" as a math formula."""
    return text.replace("$", "\\$")


def show_ai_review(review: LLMReview) -> None:
    """Display the scores, strengths, problems, rewritten bullets and keywords."""
    score_left, score_right = st.columns(2)
    score_left.metric("Overall score", f"{review.overall_score}/100")
    score_right.metric("ATS score", f"{review.ats_score}/100")

    st.markdown("#### Strengths")
    if review.strengths:
        st.markdown("\n".join(f"- {escape_markdown(s)}" for s in review.strengths))
    else:
        st.write("No strengths listed.")

    st.markdown("#### Problems")
    if not review.problems:
        st.write("No problems listed.")
    for problem in review.problems:
        with st.container(border=True):
            st.markdown(
                f"**{escape_markdown(problem.issue)}**  \n"
                f"**Why it matters:** {escape_markdown(problem.why_it_matters)}  \n"
                f"**Fix:** {escape_markdown(problem.fix)}"
            )

    st.markdown("#### Rewritten bullets")
    if review.rewritten_bullets:
        st.caption(
            "Replace placeholders like [X%] or [N users] with your real numbers. "
            "Leave them out if you don't know the number."
        )
    else:
        st.write("No bullets were rewritten.")
    for bullet in review.rewritten_bullets:
        with st.container(border=True):
            st.markdown(
                f"**Before:** {escape_markdown(bullet.original)}  \n"
                f"**→ After:** {escape_markdown(bullet.improved)}"
            )

    st.markdown("#### Missing keywords")
    if review.missing_keywords:
        st.write(", ".join(review.missing_keywords))
        st.caption("Add these only if you really have the skill or experience.")
    else:
        st.write("None. Your resume covers the key terms for this role.")


def checks_for(resume: ParsedResume, profile: UserProfile | None) -> list[Issue]:
    """run_checks(), but only re-run when the resume or profile has changed.

    The result is kept in this visitor's session_state (not a shared cache),
    so one user's resume never ends up in memory shared with other users.
    """
    inputs = (st.session_state.resume_file_id, copy.deepcopy(profile))
    memo = st.session_state.checks_memo
    if memo is None or memo[0] != inputs:
        with timed("run_checks"):
            st.session_state.checks_memo = (inputs, run_checks(resume, profile))
    return st.session_state.checks_memo[1]


def save_analysis_to_account(resume: ParsedResume, issues: list[Issue], review: LLMReview) -> None:
    """Save a finished analysis (not the resume itself) and remember how it went."""
    try:
        saved = save_analysis(
            st.session_state.resume_file_name or "resume", resume.word_count, issues, review
        )
        st.session_state.ai_review_save_note = ("success", "Saved to your account.")
        # Add it to the list on screen instead of reloading the whole list.
        if st.session_state.past_analyses is not None and not st.session_state.past_analyses_error:
            st.session_state.past_analyses = [saved] + st.session_state.past_analyses
        else:
            st.session_state.past_analyses = None  # load the list once, on the next run
    except DatabaseError as error:
        st.session_state.ai_review_save_note = ("warning", f"Not saved to your account. {error}")


def render_ai_review(resume: ParsedResume, profile: UserProfile | None, issues: list[Issue]) -> None:
    """Run the Gemini review only when the button is clicked, and show the saved result.

    A successful review is saved to the user's account automatically.
    """
    st.subheader("AI Review")
    st.caption(
        "Privacy: clicking the button sends your resume text to Google's Gemini API. "
        "On the free tier, Google may use it to improve its products."
    )

    # The review is only run on a click, and only if a daily review is left.
    if st.button("Run AI review", disabled=uses_left(AI_REVIEWS) == 0) and try_use(AI_REVIEWS):
        with st.spinner("Reviewing your resume..."):
            st.session_state.ai_review_save_note = None
            try:
                st.session_state.ai_review = review_resume(resume, profile)
                st.session_state.ai_review_error = ""
            except LLMReviewError as error:
                st.session_state.ai_review = None
                st.session_state.ai_review_error = str(error)
        if st.session_state.ai_review is not None:
            save_analysis_to_account(resume, issues, st.session_state.ai_review)
        # Remember which upload this result is for.
        st.session_state.ai_review_file_id = st.session_state.resume_file_id
    show_uses_left(AI_REVIEWS)

    # A saved review is only shown if it belongs to the resume uploaded now.
    if st.session_state.ai_review_file_id != st.session_state.resume_file_id:
        return
    if st.session_state.ai_review_error:
        st.error(st.session_state.ai_review_error)
    elif st.session_state.ai_review is not None:
        if st.session_state.ai_review_save_note:
            kind, text = st.session_state.ai_review_save_note
            {"success": st.success, "warning": st.warning}[kind](text)
        show_ai_review(st.session_state.ai_review)


# ---------- Tab 4: jobs ----------


def search_inputs(profile: UserProfile) -> tuple:
    """Everything a job search depends on, to tell if saved results are out of date."""
    return (
        profile.target_title,
        profile.location,
        profile.work_type,
        tuple(profile.job_sources),
        st.session_state.resume_file_id,
    )


def run_job_search(profile: UserProfile) -> tuple[list[Job], list[tuple[str, str]]]:
    """Search every source selected in the profile, one by one.

    A failing source becomes a message instead of stopping the others.

    Returns:
        (all jobs found, messages as (kind, text) where kind is "info" or "warning")
    """
    jobs: list[Job] = []
    messages: list[tuple[str, str]] = []
    remote_only = profile.work_type == "remote"

    for name in profile.job_sources:
        source_class = JOB_SOURCE_CLASSES.get(name)
        if source_class is None:
            continue  # "Search links" has no API to call
        if source_class is AdzunaSource:
            # A country Adzuna doesn't cover is expected, not a problem: show it as info.
            try:
                find_country(profile.location)
            except JobSourceError as error:
                messages.append(("info", str(error)))
                continue
        try:
            # Only the title is searched: long queries return few results.
            found = source_class().search(profile.target_title, profile.location, remote_only)
            jobs += found
            messages.append(("caption", f"{name}: {len(found)} job(s)"))
        except JobSourceError as error:
            messages.append(("warning", str(error)))
    return jobs, messages


def source_family(job: Job) -> str:
    """The main source of a job: "JSearch (LinkedIn)" -> "JSearch"."""
    return job.source.split(" (")[0]


def load_saved_jobs() -> list[SavedJob]:
    """Return the user's saved jobs, asking the database only when the list is stale."""
    if st.session_state.saved_jobs is None:
        try:
            st.session_state.saved_jobs = list_saved_jobs()
            st.session_state.saved_jobs_error = ""
        except DatabaseError as error:
            st.session_state.saved_jobs = []  # don't retry on every click
            st.session_state.saved_jobs_error = str(error)
    return st.session_state.saved_jobs


def notify(message: str, icon: str) -> None:
    """Queue a toast message from a button callback; show_pending_toast() displays it.

    Callbacks of buttons inside a fragment can't display anything themselves,
    so the message waits in session_state until the fragment draws it.
    """
    st.session_state.pending_toast = (message, icon)


def show_pending_toast() -> None:
    """Show (once) a toast queued by notify()."""
    pending = st.session_state.pop("pending_toast", None)
    if pending:
        message, icon = pending
        st.toast(message, icon=icon)


def save_job_clicked(item: ScoredJob) -> None:
    """Callback for a card's "Save" button."""
    try:
        newly_saved = save_job(item)
    except DatabaseError as error:
        notify(f"Not saved. {error}", "⚠️")
        return

    saved_list = st.session_state.saved_jobs
    list_ok = saved_list is not None and not st.session_state.saved_jobs_error
    if newly_saved:
        notify(f"Saved: {item.job.title}", "✅")
        if list_ok:
            # Add it to the list on screen instead of reloading the whole list.
            saved_list.insert(0, SavedJob(item.job, item.score, datetime.now(timezone.utc)))
        else:
            st.session_state.saved_jobs = None  # load the list once, on the next run
    else:
        notify("This job is already in your saved jobs.", "ℹ️")
        if not list_ok or all(saved.job.url != item.job.url for saved in saved_list):
            st.session_state.saved_jobs = None  # saved elsewhere: our list is out of date


def remove_job_clicked(job_url: str) -> None:
    """Callback for a saved job's "Remove" button."""
    try:
        remove_saved_job(job_url)
    except DatabaseError as error:
        notify(f"Not removed. {error}", "⚠️")
        return
    notify("Removed from your saved jobs.", "🗑️")
    if st.session_state.saved_jobs is not None:
        st.session_state.saved_jobs = [
            saved for saved in st.session_state.saved_jobs if saved.job.url != job_url
        ]


def show_job_card(item: ScoredJob, is_saved: bool, button_key: str) -> None:
    """Display one ranked job: score, details, skills, and Apply and Save buttons."""
    job = item.job
    with st.container(border=True):
        details, score = st.columns([4, 1])
        with details:
            st.markdown(f"**{escape_markdown(job.title)}**")
            location = job.location
            if job.remote and "remote" not in location.lower():
                location += " (remote)"
            facts = [job.company, location, job.source]
            if job.posted_date:
                facts.append(f"posted {job.posted_date:%d %b %Y}")
            st.caption(escape_markdown(" · ".join(facts)))
        score.metric("Match", f"{item.score}/100")

        st.markdown(
            f"**Matched skills:** {escape_markdown(', '.join(item.matched_skills)) or 'none'}  \n"
            f"**Missing skills:** {escape_markdown(', '.join(item.missing_skills)) or 'none'}  \n"
            f"**Experience:** {escape_markdown(item.experience_note)}"
        )

        apply_col, save_col, credit_col = st.columns([1, 1, 3], vertical_alignment="center")
        apply_col.link_button("Apply", job.url)  # opens the original posting in a new tab
        if is_saved:
            save_col.button("Saved ✓", key=button_key, disabled=True)
        else:
            save_col.button("Save", key=button_key, on_click=save_job_clicked, args=(item,))
        if source_family(job) == "Remotive":
            credit_col.caption("via [Remotive](https://remotive.com)")  # required by Remotive's terms


def show_job_results(results: list[ScoredJob]) -> None:
    """Show the filters and the job cards that pass them."""
    if not results:
        st.info("No jobs were found. Try a broader job title in tab 1.")
        return

    # A new key per search resets the filters when the result set changes.
    search_id = st.session_state.job_search_count
    families = sorted({source_family(item.job) for item in results})
    source_col, score_col = st.columns([3, 2], vertical_alignment="bottom")
    chosen = source_col.multiselect(
        "Source", families, default=families, key=f"job_filter_source_{search_id}"
    )
    min_score = score_col.slider("Minimum score", 0, 100, 0, key=f"job_filter_score_{search_id}")
    remote_col, level_col, meets_col = st.columns(3)
    remote_only = remote_col.checkbox("Remote only", key=f"job_filter_remote_{search_id}")
    hide_above = level_col.checkbox(
        "Hide jobs above my level",
        key=f"job_filter_level_{search_id}",
        help="Hides jobs that ask for more than 2 extra years, or a level 2 steps above yours. "
        "Jobs just a bit above your level stay visible.",
    )
    meets_only = meets_col.checkbox(
        "Only jobs where I meet the experience requirement",
        key=f"job_filter_meets_{search_id}",
        help="Shows jobs whose level and years you fully meet, plus jobs that state no requirement.",
    )

    shown = [
        item for item in results
        if source_family(item.job) in chosen
        and (item.job.remote or not remote_only)
        and not (hide_above and item.above_level)
        and not (meets_only and not item.meets_experience)
        and item.score >= min_score
    ]
    st.write(f"Showing {len(shown)} of {len(results)} jobs")
    saved_urls = {saved.job.url for saved in load_saved_jobs()}
    for number, item in enumerate(shown):
        show_job_card(item, item.job.url in saved_urls, f"save_job_{search_id}_{number}")


def show_search_links(profile: UserProfile) -> None:
    """Show plain search links for LinkedIn, Indeed, Glassdoor and Rozee.pk."""
    st.subheader("Search these sites yourself")
    st.caption("These open the site's own search in a new tab. Nothing is fetched by this app.")
    links = build_search_links(profile.target_title, profile.location, profile.work_type == "remote")
    for column, (site, url) in zip(st.columns(len(links)), links):
        column.link_button(site, url, width="stretch")


def show_saved_jobs() -> None:
    """Show the user's saved jobs with Apply and Remove buttons."""
    saved_jobs = load_saved_jobs()
    st.subheader(f"My saved jobs ({len(saved_jobs)})")
    if st.session_state.saved_jobs_error:
        st.warning(f"Couldn't load your saved jobs. {st.session_state.saved_jobs_error}")
        return
    if not saved_jobs:
        st.caption("No saved jobs yet. Click **Save** on any job card to keep it here.")
        return

    for number, saved in enumerate(saved_jobs):
        job = saved.job
        with st.container(border=True):
            st.markdown(f"**{escape_markdown(job.title)}**")
            facts = [job.company, f"score {saved.score}/100", f"saved {saved.saved_at:%d %b %Y}"]
            st.caption(escape_markdown(" · ".join(facts)))
            apply_col, remove_col, credit_col = st.columns([1, 1, 3], vertical_alignment="center")
            apply_col.link_button("Apply", job.url)
            remove_col.button(
                "Remove", key=f"unsave_job_{number}", on_click=remove_job_clicked, args=(job.url,)
            )
            if source_family(job) == "Remotive":
                credit_col.caption("via [Remotive](https://remotive.com)")


def render_jobs_tab() -> None:
    """The Jobs tab: the search button, then results, search links and saved jobs."""
    st.header("Jobs")
    profile = render_job_search()
    render_results_and_saved_jobs(profile)


@st.fragment
def render_results_and_saved_jobs(profile: UserProfile | None) -> None:
    """Job results with filters, search links and saved jobs.

    This is a Streamlit fragment: changing a filter or clicking Save/Remove
    reruns only this part of the page, not all four tabs. Saved jobs are inside
    it too, so saving a job updates the list below straight away.
    """
    with timed("jobs fragment (results + saved jobs)"):
        show_pending_toast()
        if profile is not None and st.session_state.job_results is not None:
            if st.session_state.job_search_inputs != search_inputs(profile):
                st.caption("Your profile or resume changed since this search. Click **Search jobs** to update.")
            for kind, text in st.session_state.job_messages:
                {"info": st.info, "warning": st.warning, "caption": st.caption}[kind](text)
            show_job_results(st.session_state.job_results)

        if profile is not None and "Search links" in profile.job_sources:
            st.divider()
            show_search_links(profile)

        st.divider()
        show_saved_jobs()  # shown even before any search


def render_job_search() -> UserProfile | None:
    """Show what will be searched and the "Search jobs" button; run the search on click.

    Returns:
        The profile, or None if a resume or saved profile is still missing.
    """
    if st.session_state.resume is None:
        st.info("Upload your resume in tab 2 to find matching jobs.")
        return None
    profile = st.session_state.profile
    if profile is None:
        st.info("Fill in and save your profile in tab 1 first. The job search uses your target title and location.")
        return None

    st.markdown(
        f"**Searching for:** {escape_markdown(profile.target_title)}  \n"
        f"**Experience:** {profile.experience_level.capitalize()}, "
        f"{profile.years_experience:g} year(s)  \n"
        f"**Location:** {escape_markdown(profile.location) or 'any'}  \n"
        f"**Work type:** {profile.work_type.capitalize()}"
    )

    api_sources = [name for name in profile.job_sources if name in JOB_SOURCE_CLASSES]
    if not api_sources:
        st.info("No job sources with search results are selected in tab 1.")
    else:
        # Searching uses limited free API quotas: only on a click, and only if a search is left.
        clicked = st.button("Search jobs", type="primary", disabled=uses_left(SEARCHES) == 0)
        if clicked and try_use(SEARCHES):
            with st.spinner(f"Searching {', '.join(api_sources)}..."):
                jobs, messages = run_job_search(profile)
                with timed("rank_jobs"):
                    st.session_state.job_results = rank_jobs(
                        jobs, st.session_state.resume, profile
                    )
            st.session_state.job_messages = messages
            st.session_state.job_search_inputs = search_inputs(profile)
            st.session_state.job_search_count += 1
        show_uses_left(SEARCHES)
    return profile


# ---------- Login screen and sidebar ----------


def render_login_screen() -> None:
    """Show the "Log in" and "Sign up" forms. Nothing else is shown until login."""
    message = st.session_state.pop("login_message", None)  # e.g. after deleting an account
    if message:
        st.success(message)
    st.write("Log in or create a free account to analyze your resume and find matching jobs.")
    login_tab, signup_tab = st.tabs(["Log in", "Sign up"])

    with login_tab:
        # A form sends everything in one go when the button is pressed (or Enter).
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Log in", type="primary")
        if submitted:
            try:
                sign_in(email, password)
                st.rerun()  # show the app now that this visitor is logged in
            except AuthError as error:
                st.error(str(error))

    with signup_tab:
        with st.form("signup_form"):
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_password")
            confirm = st.text_input("Confirm password", type="password", key="signup_confirm")
            st.caption("At least 8 characters.")
            submitted = st.form_submit_button("Create account", type="primary")
        if submitted:
            if password != confirm:
                st.error("The two passwords don't match.")
            else:
                try:
                    if sign_up(email, password):
                        st.rerun()
                    else:
                        st.success(
                            "Account created! We've sent you a confirmation email. "
                            "Click the link in it, then log in."
                        )
                except AuthError as error:
                    st.error(str(error))


def log_out() -> None:
    """Log out and clear EVERYTHING this visitor had in session_state."""
    sign_out()
    st.session_state.clear()  # profile, resume, results, filters: nothing is left behind


def ask_to_confirm(action: str | None) -> None:
    """Callback: start ("data"/"account") or cancel (None) the "Are you sure?" step."""
    st.session_state.confirm_delete = action
    st.session_state.account_message = None


def confirm_delete_clicked() -> None:
    """Callback for "Yes, delete": delete the user's data or whole account."""
    action = st.session_state.confirm_delete
    st.session_state.confirm_delete = None
    if action == "data":
        try:
            delete_my_data()
            # Forget the local copies so nothing deleted is shown as saved.
            st.session_state.saved_jobs = None
            st.session_state.past_analyses = None
            st.session_state.account_message = (
                "success", "Your saved profile, analyses and saved jobs were deleted."
            )
        except DatabaseError as error:
            st.session_state.account_message = ("warning", f"Not everything was deleted. {error}")
    elif action == "account":
        try:
            delete_my_account()
        except DatabaseError as error:
            st.session_state.account_message = ("warning", f"Your account wasn't deleted. {error}")
            return
        log_out()  # clears this visitor's session_state
        st.session_state.login_message = "Your account and all its data were deleted."


def render_account_section() -> None:
    """The "My account" part of the sidebar: delete data or the whole account."""
    st.divider()
    st.subheader("My account")
    if st.session_state.account_message:
        kind, text = st.session_state.account_message
        {"success": st.success, "warning": st.warning}[kind](text)

    action = st.session_state.confirm_delete
    if action is None:
        st.button("Delete my data", on_click=ask_to_confirm, args=("data",))
        st.button("Delete my account", on_click=ask_to_confirm, args=("account",))
        return

    if action == "data":
        st.warning(
            "Are you sure? This deletes your saved profile, analyses and saved jobs. "
            "This cannot be undone."
        )
        yes_label = "Yes, delete my data"
    else:
        st.warning(
            "Are you sure? This deletes your account and everything saved in it. "
            "This cannot be undone."
        )
        yes_label = "Yes, delete my account"
    st.button(yes_label, type="primary", on_click=confirm_delete_clicked)
    st.button("Cancel", on_click=ask_to_confirm, args=(None,))


def render_sidebar(user: AuthUser) -> None:
    """Show who is logged in, a "Log out" button and the account section."""
    with st.sidebar:
        st.text(f"Logged in as {user.email}")  # plain text: emails can contain Markdown characters
        st.button("Log out", on_click=log_out)
        render_account_section()


# ---------- Page layout ----------

st.set_page_config(page_title="AI Resume Analyzer", page_icon="📄", layout="centered")
st.title("AI Resume Analyzer")

with timed("script run (whole page)"):
    user = current_user()
    if user is None:
        render_login_screen()
        st.stop()  # the four app tabs are only shown to logged-in visitors

    init_session_state()
    render_sidebar(user)
    load_saved_profile()  # once per login, before the form widgets are drawn

    profile_tab, upload_tab, analysis_tab, jobs_tab = st.tabs(
        ["1. Your Profile", "2. Upload Resume", "3. Analysis", "4. Jobs"]
    )

    with profile_tab, timed("tab 1 profile"):
        render_profile_tab()

    with upload_tab, timed("tab 2 upload"):
        render_upload_tab()

    with analysis_tab, timed("tab 3 analysis"):
        render_analysis_tab()

    with jobs_tab, timed("tab 4 jobs"):
        render_jobs_tab()
