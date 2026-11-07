---
name: data-modeling
description: Plan schema, migration, and data-model changes before implementing them. Use whenever a task adds or changes tables/collections/fields/relationships or indexes, introduces a migration, or adds a query that scans significant data. Primary users are architect (design) and senior-dev (before implementing); skip for changes that don't touch persisted data shape.
---

# Data modeling

Data-model mistakes are the hardest to reverse once real data exists — plan before code.

1. **Read the ACTUAL schema** — migrations, ORM models, `sqlite3 <db> .schema`, `psql -c '\d'`. Never trust documentation alone.
2. **Backward compatibility**: preserve existing data unless the task explicitly requires a breaking change — and a breaking change requires a written migration AND rollback plan, called out as breaking.
3. **Integrity + concurrency**: uniqueness, foreign keys, required fields, cascade behavior; races on concurrent writes; transaction boundaries.
4. **Performance**: recommend indexes/denormalization only for access patterns actually observed in the code — never speculative optimization.
5. **Output before implementing** (plan, not code): relevant current-schema summary → exact proposed fields/types/constraints → integrity + concurrency notes → migration/rollback plan → risks/edge cases.

Senior-dev implements what the plan says; deviations from the plan go back through architect (CONSULT), not silently.
