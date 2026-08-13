# Audit outputs

Hourly audit summaries (JSON). Each run writes:

| Field | Meaning |
|-------|---------|
| `counts.total_scanned` | Total dataset records traversed (paginated) |
| `counts.compliant` | Records whose license matches policy |
| `counts.violations` | Prohibited / missing licenses |
| `counts.needs_review` | Licenses requiring human review |
| `counts.changed` | README declaration differs from license tag |
| `issues.created / updated / closed` | GitHub issue sync stats |
| `elapsed_seconds` | Run duration |
