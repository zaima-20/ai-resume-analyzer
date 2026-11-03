# AI Resume Analyzer: Project Spec

This file tells Claude Code what we are building. Read it before every task.

## Goal

A Streamlit web app that:
1. Asks the user about their skills and career goals
2. Analyzes their uploaded resume and lists concrete problems with fixes
3. Finds matching jobs from several job sources and ranks them by fit

This is a portfolio project. Code should be clean, beginner-to-intermediate level, well commented, and easy to explain in an interview.

## Tech stack

- Python 3.11+
- Streamlit for the UI (no HTML/CSS/JS frameworks)
- pdfplumber (PDF) and python-docx (DOCX) for resume parsing
- Google Gemini API (`google-genai` package) for the LLM, free tier
- requests for job APIs
- python-dotenv for API keys
- pytest for tests

## Project structure

```
ai-resume-analyzer/
├── app.py                  # Streamlit UI only, no business logic
├── src/
│   ├── __init__.py
│   ├── config.py           # loads .env, constants
│   ├── profile.py          # skills questionnaire data model (dataclass)
│   ├── parser.py           # extract text + sections from PDF/DOCX
│   ├── rule_checks.py      # deterministic checks (no LLM)
│   ├── llm_review.py       # Gemini review, returns structured JSON
│   ├── jobs/
│   │   ├── __init__.py
│   │   ├── base.py         # common Job dataclass + JobSource interface
│   │   ├── jsearch.py      # JSearch API (LinkedIn, Indeed, Glassdoor via Google for Jobs)
│   │   ├── adzuna.py       # Adzuna API
│   │   ├── remotive.py     # Remotive API (remote jobs, no key)
│   │   └── search_links.py # direct search URLs for LinkedIn, Indeed, Rozee.pk, Glassdoor
│   ├── matcher.py          # score and rank jobs against resume + profile
│   └── cache.py            # simple JSON file cache to save API quota
├── tests/
├── sample_data/            # a fake sample resume for testing
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Feature details

### 1. Skills questionnaire (profile.py + app.py)
Collect: target job title, experience level (student / junior / mid / senior), years of experience, top skills with self-rating 1-5, preferred location, remote/onsite/hybrid, and preferred job sources. Store in a `UserProfile` dataclass in `st.session_state`.

### 2. Resume parsing (parser.py)
- Accept PDF and DOCX up to 5 MB
- Return raw text plus detected sections (Summary, Experience, Education, Skills, Projects, Certifications) using heading keyword matching
- Extract email, phone, LinkedIn and GitHub links with regex
- Handle scanned/empty PDFs gracefully: show a clear error, don't crash

### 3. Resume analysis
**Rule checks (rule_checks.py), no LLM, fast and free:**
- Missing sections or contact details
- Length (word count, estimated pages)
- Bullets with no numbers/metrics
- Weak verbs ("responsible for", "worked on", "helped")
- Skills the user claimed in the questionnaire but that never appear in the resume
- Each check returns: issue, severity (high/medium/low), where it was found, suggested fix

**LLM review (llm_review.py):**
- Send resume text + profile to Gemini
- Ask for JSON only: overall score 0-100, ATS score 0-100, strengths, problems (issue, why it matters, fix), 3 rewritten bullet points, missing keywords for the target role
- Validate the JSON; retry once if invalid; show a friendly error if it still fails
- Never invent experience the user does not have

### 4. Job search (jobs/)
- All sources return the same `Job` dataclass: title, company, location, remote (bool), description, url, source, posted_date
- Build the search query from the target title + top skills + location
- Run sources independently; if one fails or has no key, skip it and show a warning, never crash
- Do NOT scrape LinkedIn, Indeed or Glassdoor directly (against their terms). Their postings are reached through JSearch. `search_links.py` only builds normal search URLs the user opens in their browser.
- Cache results for 6 hours (cache.py) because free tiers have small monthly quotas
- JSearch uses the /search-v2 endpoint; the old /search endpoint was retired.

### 5. Matching (matcher.py)
- Score each job 0-100: keyword overlap between resume skills and job description (45%), title similarity (20%), experience fit (20%), location/remote match (15%)
- Experience fit compares the job's seniority (from its title/description) and required years (e.g. "3+ years") with the profile's experience level and years; each job gets an experience note
- Show matched skills and missing skills per job
- Sort by score, remove duplicates (same title + company)

### 6. UI (app.py)
Four tabs: "1. Your Profile", "2. Upload Resume", "3. Analysis", "4. Jobs".
- Analysis tab: scores as metrics, problems grouped by severity, rewritten bullets
- Jobs tab: filters (source, remote only, minimum score), job cards with score, matched/missing skills and an "Apply" link to the original site
- Download button: analysis report as Markdown

## Rules for Claude Code
- Work in small steps; one module per task unless asked otherwise
- Never hard-code API keys; read from `.env`
- Add type hints and docstrings to every function
- Write pytest tests for parser, rule_checks and matcher (mock all API calls)
- After changing code, run the tests
- Keep `app.py` thin: UI only, logic lives in `src/`
- Explain what you changed in plain words when done
