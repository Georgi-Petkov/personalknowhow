# Contributing

PersonalKnowHow is primarily a personal project, built fork-and-populate style — clone it,
drop in your own exports, and it's yours. That said:

- **Bugs and ideas**: open a GitHub issue. Real bug reports (especially "the pipeline crashed
  on X" with the actual error) are genuinely useful.
- **Pull requests**: welcome for bug fixes, new ingest sources, or generalizing something that's
  currently too specific to one person's data. Please open an issue first for anything larger
  than a small fix, so we can agree on direction before you put in the work.
- **New ingest sources**: the schema in `ingest/common.py` (`{title, type, provider, date,
  description, domain_tags}`) is the contract every source script writes to. A new source that
  follows this schema and writes to its own `corpus/<source>/` subfolder should slot in cleanly.

No CLA, no formal process — just be reasonable and explain your change.
