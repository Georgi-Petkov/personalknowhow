# personalknowhow.com — Hero section copy update

## Instructions for Claude Code

Update the hero section and add a sample-output block. Do not touch "How it works,"
privacy, or waitlist sections. Preserve existing links, MCP config block, and curl
command — just move them lower if needed so a static example appears before them.

## 1. Replace headline + subhead

**Current:**
> Stop guessing what you know. Query it instead.
>
> PersonalKnowHow turns a scattered learning and work history — LinkedIn, GitHub,
> course platforms, project repos — into a unified knowledge graph, served over real
> MCP servers with semantic search. Ask "do I have Django experience?" and get back
> actual evidence with a similarity score, not a keyword match.

**Replace with:**
> Your resume is a story. This is the evidence.
>
> A resume tells a story about what you know. PersonalKnowHow builds a knowledge graph
> from what actually happened — your GitHub commits, course completions, project
> history — and lets you query it directly. Ask "do I have Django experience?" and get
> back real evidence with a similarity score, not a claim.

(Drop "served over real MCP servers" from the subhead — it's implementation detail,
not the pitch. It already lives in "How it works.")

## 2. Add a static example block

Insert directly after the hero (before the "Course lists and resumes are a weak
signal" section). This is a static, hardcoded example — not a live query — just to
show the payoff before asking anyone to set up MCP.

```
Query: "Do I have experience with distributed systems?"

Evidence returned:
→ 0.91  GitHub — kafka-consumer-pool (47 commits, 2023–2024)
→ 0.84  DataCamp — "Distributed Computing with PySpark" (completed 2024-03)
→ 0.79  LinkedIn — Endorsement: "Scaled our event pipeline" (colleague, 2023)

Not a keyword match. Not self-reported. Ranked by semantic similarity to real
provenance-tagged evidence.
```

Style this consistent with existing site fonts/monospace blocks used for the JSON/curl
examples further down.

## 3. Leave unchanged

- "Course lists and resumes are a weak signal" section — still valid, still true, can
  stay word-for-word.
- "How it works" 4-step section — unchanged.
- Demo/MCP config/curl instructions — unchanged, just push down below the new
  example block. Note the file gives a static example; the config below the fold is for
  people who want the live version.
- Privacy and waitlist sections — unchanged.
