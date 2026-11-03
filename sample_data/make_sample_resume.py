"""Builds sample_data/sample_resume.docx for a made-up person (no real data).

Run from the project root:  python sample_data/make_sample_resume.py
The tests in tests/test_parser.py depend on this content, so update them if you change it.
"""

from pathlib import Path

import docx

OUTPUT_PATH: Path = Path(__file__).parent / "sample_resume.docx"


def build_sample_resume(path: Path) -> None:
    """Write a fake one-page resume for "Jordan Example" to the given path.

    The contact details are fictional (example.com email, 555 phone number).
    Some bullets use weak verbs or have no numbers on purpose, so the rule checks
    have something to find.
    """
    doc = docx.Document()

    doc.add_heading("Jordan Example", level=0)
    doc.add_paragraph(
        "Karachi, Pakistan | jordan.example@example.com | +1 (555) 010-4477 | "
        "linkedin.com/in/jordan-example-demo | github.com/jordan-example-demo"
    )

    doc.add_heading("Professional Summary", level=1)
    doc.add_paragraph(
        "Junior data analyst with 1.5 years of experience turning sales and operations "
        "data into clear reports. Comfortable with SQL, Python and Excel."
    )

    doc.add_heading("Work Experience", level=1)
    doc.add_paragraph("Data Analyst Intern, Sample Retail Co. | Jan 2024 - Present")
    doc.add_paragraph("Built 6 weekly sales dashboards in Excel used by 25 store managers", style="List Bullet")
    doc.add_paragraph("Responsible for cleaning customer data every month", style="List Bullet")
    doc.add_paragraph("Automated a reporting task with Python, saving 4 hours per week", style="List Bullet")
    doc.add_paragraph("Operations Assistant, Demo Logistics Ltd. | Jun 2023 - Dec 2023")
    doc.add_paragraph("Worked on inventory spreadsheets for the warehouse team", style="List Bullet")
    doc.add_paragraph("Helped the manager prepare quarterly reports", style="List Bullet")

    doc.add_heading("Education", level=1)
    doc.add_paragraph("BS Computer Science, Example University | 2019 - 2023")

    doc.add_heading("Skills:", level=1)  # trailing colon on purpose, to test heading matching
    doc.add_paragraph("SQL, Python, Excel, Pandas, Data Visualization, Communication")

    doc.add_heading("Projects", level=1)
    doc.add_paragraph("Sales Forecast Dashboard: predicted monthly sales with 85% accuracy using Python", style="List Bullet")
    doc.add_paragraph("Worked on a small website for a student club", style="List Bullet")

    doc.add_heading("Certifications", level=1)
    doc.add_paragraph("Google Data Analytics Certificate (2023)")

    doc.save(path)


if __name__ == "__main__":
    build_sample_resume(OUTPUT_PATH)
    print(f"Saved {OUTPUT_PATH}")
