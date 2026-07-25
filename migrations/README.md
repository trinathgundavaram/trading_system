# Migrations

Alembic arrives in Phase 5 step 5.1 (§28). Until then, schema changes are plain
SQL files applied in filename order, recorded here so that `scripts/tp install`
can bring a fresh per-version database up to the schema that version expects.

Each file must contain **forward and backward** SQL. The `rollback_safe` field
in a release note is a claim about whether the previous version can still read
the database after the forward migration — answering that honestly requires the
backward SQL to exist.
