# PersonalKnowHow MCP — private tier

Bearer-token-gated MCP server over the **full, unfiltered** export of one person's knowledge
graph (`private_entries.json` + `private_embeddings.json`, built by
`ingest/build_private_export.py`) — includes career-interest/job-application "signal" evidence
alongside real demonstrated experience. Every result carries `evidence_tier`
(`"demonstrated"` vs `"signal_only"`) so a caller can never mistake a saved job or a stated
interest for proof of ability.

**Auth**: every request needs `Authorization: Bearer <PRIVATE_MCP_TOKEN>`. No token, or the
wrong one, returns `401`.

## Tools

### `query_knowhow(topic: string)`
Semantic search, ranked by relevance, capped at 10 results. Use for open-ended "do I know
anything about X" questions. Not exhaustive — a real match can rank below the cutoff.

```
query_knowhow({ topic: "django" })
→ { "found": true, "count": 4, "evidence": [
      { "id": "github_django_docker_db", "label": "django_docker_db", "type": "project",
        "evidence_tier": "demonstrated", "score": 0.735, ... },
      ...
   ] }
```

### `list_by_type(type: string)`
The complete, exact set of entries for one type — no ranking, no cap. Use for "list all X" /
"how many X" questions.

```
list_by_type({ type: "certification" })
→ { "type": "certification", "count": 26, "entries": [ { "id": "...", "evidence_tier": "demonstrated", ... }, ... ] }
```

### `related_entries(id: string)`
Given an entry id, returns other entries sharing at least one tag or the same provider —
capped at 15 per group, true count always shown. Not a relevance judgment — two entries can
share a broad tag and be otherwise unrelated.

```
related_entries({ id: "github_django_docker" })
→ { "id": "github_django_docker", "evidence_tier": "demonstrated",
    "related_by_tag": { "docker": { "count": 4, "entries": [ ... ] }, ... },
    "related_by_provider": { "count": 15, "entries": [ ... ] } }
```

### `skill_evidence(tag: string)`
**Exact** tag match (not semantic), uncapped, grouped by `evidence_tier` then by `type` with
real counts. The right tool for "how much real evidence do I have for X" or "is this ONLY
saved/applied signal, with nothing demonstrated" — the kind of question `query_knowhow` can't
answer because similarity search always ranks *something*, even when the true answer is zero.
`by_evidence_tier.demonstrated.count === 0` means no real evidence exists for that tag, even if
`signal_only` entries do.

Real, confirmed-live examples:

```
skill_evidence({ tag: "docker" })
→ {
    "tag": "docker", "found": true, "count": 5,
    "by_evidence_tier": {
      "demonstrated": {
        "count": 5,
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
      },
      "signal_only": { "count": 0, "by_type": {} }
    }
  }
```

```
skill_evidence({ tag: "gcp" })
→ {
    "tag": "gcp", "found": true, "count": 3,
    "by_evidence_tier": {
      "demonstrated": {
        "count": 2,
        "by_type": {
          "position": { "count": 1, "undated_count": 0, "entries": [
            { "id": "linkedin_positions_data_engineer_data_analyst_at_biites_202107",
              "label": "Data Engineer & Data Analyst at Biites", "captured_at": "2021-07-01" } ] },
          "project": { "count": 1, "undated_count": 0, "entries": [
            { "id": "projects_connector", "label": "connector", "captured_at": "2026-08-09" } ] }
        }
      },
      "signal_only": {
        "count": 1,
        "by_type": {
          "career_interest": { "count": 1, "undated_count": 0, "entries": [
            { "id": "linkedin_career_interests_cloud_developers_aws_gcp_or_azure_machine_learning_ai_t_202010",
              "label": "Cloud Developers - AWS, GCP or Azure (Machine Learning / AI) at Tech People",
              "captured_at": "2020-10-29" } ] }
        }
      }
    }
  }
```

An unrecognized tag returns `found: false` — this is an exact match against the tags already
assigned during ingest, not a fuzzy/semantic lookup. If a skill genuinely exists in the corpus
but was never tagged, `query_knowhow` may still find it (it searches full entry text, not just
tags).
