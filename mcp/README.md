# PersonalKnowHow MCP — public demo

Unauthenticated MCP server over the **public** export of one person's knowledge graph
(`public_entries.json` + `public_embeddings.json`, built by `ingest/build_public_export.py`'s
fail-closed allowlist). Every entry here represents something actually done or completed
(course, project, certification, position, education, etc.) — career-interest/job-application
"signal" evidence is structurally excluded from this export, never just filtered at query time.

## Tools

### `query_knowhow(topic: string)`
Semantic search, ranked by relevance, capped at 10 results. Not exhaustive — a real match can
rank below the cutoff.

```
query_knowhow({ topic: "django" })
→ { "found": true, "count": 4, "evidence": [
      { "id": "github_django_docker_db", "label": "django_docker_db", "type": "project", "score": 0.735, ... },
      { "id": "github_django_docker", "label": "django_docker", "type": "project", "score": 0.71, ... },
      { "id": "github_startup", "label": "StartUp", "type": "project", "score": 0.706, ... },
      { "id": "github_news_sentiment_app", "label": "News-sentiment-app", "type": "project", "score": 0.676, ... }
   ] }
```

### `list_by_type(type: string)`
The complete, exact set of entries for one type — no ranking, no cap.

```
list_by_type({ type: "certification" })
→ { "type": "certification", "count": N, "entries": [ { "id": "...", "label": "...", ... }, ... ] }
```

### `related_entries(id: string)`
Given an entry id, returns other entries sharing at least one tag or the same provider — capped
at 15 per group, true count always shown.

```
related_entries({ id: "github_django_docker" })
→ { "id": "github_django_docker", "type": "project",
    "related_by_tag": {
      "docker": { "count": 4, "entries": [
        { "id": "github_django_docker_db", "label": "django_docker_db", "type": "project" }, ... ] },
      "aws": { "count": 16, "entries": [ ... ] },
      "cloud": { "count": 51, "entries": [ ... ] },
      "devops": { "count": 10, "entries": [ ... ] }
    },
    "related_by_provider": { "count": 36, "entries": [ ... ] } }
```
(Note: this example shows `related_entries` only clustering `django_docker` with the one other
Django repo sharing an unrelated tag — neither `StartUp` nor `News-sentiment-app` appear, since
neither shares a tag with it. `skill_evidence({ tag: "docker" })` below finds all of them
instead, since it matches on the tag directly rather than requiring a shared-tag path from one
specific starting entry.)

### `skill_evidence(tag: string)`
**Exact** tag match (not semantic), uncapped, grouped by `type` with real counts. The right tool
for "how many X have I actually done" or "do I have any real evidence for X at all."

```
skill_evidence({ tag: "docker" })
→ {
    "tag": "docker", "found": true, "count": 5,
    "by_type": {
      "project": {
        "count": 4, "undated_count": 0,
        "entries": [
          { "id": "github_django_docker", "label": "django_docker", "captured_at": "2021-06-10" },
          { "id": "github_django_docker_db", "label": "django_docker_db", "captured_at": "2021-06-22" },
          { "id": "github_bedex", "label": "BedEx", "captured_at": "2025-05-29" },
          { "id": "github_terraform_associate_prep", "label": "terraform-associate-prep", "captured_at": "2026-08-03" }
        ]
      },
      "endorsement": {
        "count": 1, "undated_count": 0,
        "entries": [
          { "id": "linkedin_endorsements_docker_products_202209", "label": "Endorsed: Docker Products", "captured_at": "2022-09-21" }
        ]
      }
    }
  }
```

```
skill_evidence({ tag: "gcp" })
→ {
    "tag": "gcp", "found": true, "count": 2,
    "by_type": {
      "position": { "count": 1, "undated_count": 0, "entries": [
        { "id": "linkedin_positions_data_engineer_data_analyst_at_biites_202107",
          "label": "Data Engineer & Data Analyst at Biites", "captured_at": "2021-07-01" } ] },
      "project": { "count": 1, "undated_count": 0, "entries": [
        { "id": "projects_connector", "label": "connector", "captured_at": "2026-08-09" } ] }
    }
  }
```
(The private tier's `skill_evidence({ tag: "gcp" })` also surfaces a `signal_only` career-interest
entry — that entire category is excluded from this public export, so this tier's `count` is
lower by design, not by omission.)

An unrecognized tag returns `found: false` — exact match against tags assigned during ingest,
not a fuzzy/semantic lookup. If a skill genuinely exists but was never tagged, `query_knowhow`
may still find it (it searches full entry text, not just tags).
