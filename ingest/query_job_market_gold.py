#!/usr/bin/env python3
"""Ad hoc: query DataJobs' Gold tables for real job-market analysis.

PKH's only remaining connection to AS3 job-posting data — no local files,
no scraping (that moved entirely to DataJobs/.claude/skills/as3jobs/). This
is the Gold-table-backed successor to what analyze_job_postings.py does for
LinkedIn via local regex parsing, but for AS3 data via real dbt-deduped,
classified Gold tables instead.

Auth: token via env vars, same values as DataJobs/dbt/datajobs/.env — same
workspace and warehouse, different project. Unlike healthkit's
check_dropped_rows.py (auth_type=azure-cli, no token in the URL), token
auth requires the token embedded as the connection URL's password
component — the databricks-sqlalchemy dialect reads it via `url.password`
directly (confirmed in databricks/sqlalchemy/base.py's create_connect_args,
not passed via connect_args like the azure-cli variant).

Usage:
    set -a; source .env; set +a
    python3 ingest/query_job_market_gold.py
"""
import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

DATABRICKS_HOST = os.environ["DATABRICKS_HOST"]
HTTP_PATH = os.environ["DATABRICKS_HTTP_PATH"]
DATABRICKS_TOKEN = os.environ["DATABRICKS_TOKEN"]

CATEGORY_QUERY = """
SELECT job_category, count(*) AS n
FROM workspace.datajobs_gold.fct_postings_for_evaluation
GROUP BY job_category
ORDER BY n DESC
"""

TECHNOLOGY_QUERY = """
SELECT technology, count(*) AS n
FROM workspace.datajobs_gold.fct_posting_technologies
LATERAL VIEW explode(mentioned_technologies) AS technology
GROUP BY technology
ORDER BY n DESC
"""

SALARY_QUERY = """
SELECT title, employer, salary_range_raw, job_category
FROM workspace.datajobs_gold.fct_postings_with_salary
ORDER BY title
"""

COMPANY_TREND_QUERY = """
SELECT employer, sum(posting_count) AS n
FROM workspace.datajobs_gold.fct_company_posting_trends
GROUP BY employer
ORDER BY n DESC
LIMIT 15
"""


def main() -> None:
    url = URL.create(
        "databricks",
        username="token",
        password=DATABRICKS_TOKEN,
        host=DATABRICKS_HOST,
        query={"http_path": HTTP_PATH},
    )
    engine = create_engine(url)
    with engine.connect() as conn:
        categories = conn.execute(text(CATEGORY_QUERY)).fetchall()
        technologies = conn.execute(text(TECHNOLOGY_QUERY)).fetchall()
        salaries = conn.execute(text(SALARY_QUERY)).fetchall()
        companies = conn.execute(text(COMPANY_TREND_QUERY)).fetchall()

    print("Job category breakdown (fct_postings_for_evaluation):")
    for r in categories:
        print(f"  {r.job_category}: {r.n}")

    print("\nTechnology/skill demand (fct_posting_technologies, all mentions):")
    for r in technologies:
        print(f"  {r.technology}: {r.n}")

    print(f"\nPostings with a disclosed salary range ({len(salaries)}):")
    for r in salaries:
        print(f"  [{r.job_category}] {r.title} @ {r.employer}: {r.salary_range_raw!r}")

    print("\nTop employers by posting count (fct_company_posting_trends, all months):")
    for r in companies:
        print(f"  {r.employer}: {r.n}")


if __name__ == "__main__":
    main()
