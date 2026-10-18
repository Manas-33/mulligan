-- After the seed: audit every table in public and let mulligan_ro read them.
SELECT mulligan_audit.enable_schema('public');
