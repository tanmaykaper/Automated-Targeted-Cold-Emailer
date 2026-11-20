# inputs/ — drop lead CSVs here

Drop any of these and commit/push (or run `python -m outreach tick` locally):

- **Apollo** "Export contacts" CSV (all columns as exported — email status, catch-all status, website are used)
- **LinkedIn / Sales Navigator** export (First Name, Last Name, Company, Position, URL …)
- **Any sheet** with at least a name column and a company column (title, email, LinkedIn URL, website, location optional)

Columns are matched case-insensitively by alias. Every row is deduplicated against everyone already in the
pipeline (by LinkedIn URL, email, and name + company) — including everyone v1 already emailed — then goes
through verification → research → scoring → drafting. Processed files move to `inputs/processed/<date>/`;
byte-identical re-uploads are skipped.

Rows whose email can't be verified land in `state/needs_enrichment.csv` instead of the send queue.
