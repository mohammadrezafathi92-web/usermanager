# Explicit runtime ownership CHECK v2

The owner explicitly authorized this controlled exception to additive-only
schema changes. It is code/test only: **no startup migration, live route,
deployment command, mode activation or server execution is added**. The model
still creates v1 tables on a fresh install; v2 is an explicit maintenance step
for the future authenticated provisioning controller.

Only `ck_provrt_enforced_needs_owner` changes, adding `released` to the permitted
owner states while the gate stays enforced. The other CHECKs still require
released ownership to have NULL host/boot/timestamps. `none` plus enforced is
still invalid. Host revalidation requires **active** ownership, so the interval
between release and the new host's claim remains fail-closed, not shadow/off.

The schema inspector accepts only the exact known v1 or exact v2 expression,
under the same exact name; it still rejects missing/extra CHECKs and arbitrary
wider expressions. Other tables/constraints are unchanged. Release inspects
the actual CHECK and refuses old-schema enforced handoff, never silently
downgrading the mode.

The upgrade helper requires a held exclusive installation mode lock, matching
installation/version, superadmin ID, verified known schema, gate off, vacant
ownership, all type modes legacy and no nonterminal operation. The future
controller must authenticate the password and run it during single-host
maintenance, before claiming/activating durable ownership. Unknown runtime
indexes, SQLite triggers/views or incoming foreign references are refused;
none are dropped or guessed at. A backup and maintenance quiescence are
operational prerequisites; this internal helper is not a public arbitrary
database migration endpoint.

SQLite uses an explicit writer transaction, creates an exact replacement table,
copies every column, checks equality, replaces the old table and verifies the
new schema plus unchanged foreign-key violations before committing. No
`writable_schema` or disabling FK checks. Injected faults after copy, after
DROP and after rename restore v1 and all data.

MariaDB >=10.6 uses a single ALTER replacing the named CHECK; not a separate
DROP/ADD window. Its completed DDL implicitly commits: an observer failure
afterwards is **not** described as transactional rollback. Retry verifies the
exact v2 schema and unchanged row, then is a no-op. Older MariaDB and genuine
MySQL upgrades are refused until separately verified. SQLite and MariaDB's
procedures follow their [SQLite ALTER documentation](https://www.sqlite.org/lang_altertable.html)
and [MariaDB ALTER documentation](https://mariadb.com/docs/server/reference/sql-statements/data-definition/alter/alter-table).

`test_provisioning_runtime_upgrade.py` uses disposable SQLite and mandatory CI
MariaDB, proves unchanged runtime UUID/version/timestamps and customer data,
fault recovery and retry, exact inspector compatibility, genuine enforced
handoff without mode downgrade, and refusal of wider/extra CHECKs.

Deployment order: install this compatible code while everything is inert;
perform the explicit approved upgrade; only later enable durable ownership
after all other P6 prerequisites. **After enforcing v2, rollback to code predating
v2 compatibility is unsupported**: its readiness/gate logic does not understand
the new schema. No activation controller exists in this batch and none of
these operations has been performed on the user's server.
