# SchemaForge - build your own database without writing SQL

A no-code database builder (CSC2131 RDBMS lab project). A user signs in, creates a database, designs
tables and constraints in the browser, links them with relationships, enters data, queries it,
shares it with other users, and exports it as SQL for SQLite, PostgreSQL, MySQL or Oracle.

Each user-made database is a **real separate SQLite file**, so nothing is simulated.
Oracle is only one optional *export target*; nothing in the app requires it.

## Run it

```bash
pip install flask          # the only dependency
python app.py              # http://127.0.0.1:5000
python -m unittest discover -s tests -v    # 9 end-to-end tests
```

Register an account, click **New database**, then use the tabs.

## What each tab does (and which lab it covers)

| Tab | What it does | Lab / CO |
|---|---|---|
| Design | New table, columns, PK / FK / NOT NULL / UNIQUE / CHECK / DEFAULT, indexes, rename, drop, alter column (safe rebuild), 1:1 / 1:N / M:N relationship wizard, live ER diagram, every DDL statement shown | 1, 3, 4 (CO1, CO2) |
| Data | Forms generated from the schema, type validation, FK dropdowns, search, sort, paging, CSV import (all-or-nothing) | 5 (INSERT/UPDATE/DELETE) |
| Query builder | SELECT, DISTINCT, WHERE (AND/OR, LIKE, IN, BETWEEN, IS NULL), INNER/LEFT/RIGHT/FULL/CROSS and self joins, aggregates, GROUP BY, HAVING, ORDER BY, LIMIT, single-row / multi-row / correlated subqueries, EXISTS | 6-9 (CO3) |
| SQL & transactions | Free SQL console (UNION, MERGE-style upserts...), BEGIN, SAVEPOINT, ROLLBACK TO, COMMIT, ROLLBACK | 12 (CO4) |
| Routines | Saved parameterised SQL = stored procedures; each call is atomic | 12 (CO5) |
| Views | Create from the builder or SQL, query, drop, grant access to a view | 10 |
| Normalize | Declare functional dependencies -> candidate keys, 2NF/3NF/BCNF violations, minimal cover, lossless 3NF + BCNF decomposition with ready-to-run SQL | 1 (CO1) |
| Access | Add members, GRANT / REVOKE SELECT, INSERT, UPDATE, DELETE per table or view; enforced by the engine | 11 (CO4) |
| History & audit | Trigger-written audit trail (old/new values) and a log of every DDL/DML/DCL/TCL statement | 12 (triggers) |
| Export | `.sql` for SQLite / PostgreSQL / MySQL / Oracle incl. data, views, GRANTs, audit trigger, **cursor procedure** (PL/pgSQL, PL/SQL), transaction demo; or download the SQLite file | CO5 |

## The platform's own relational model (your ER diagram + relational mapping)

Entities: **User, Project, Project_Member, Grant, Functional_Dep, Routine, Activity_Log**

```
users(user_id PK, username UNIQUE, pw_hash, created_at)
projects(project_id PK, owner_id FK->users, name, description, created_at, UNIQUE(owner_id,name))
project_members(project_id FK, user_id FK, added_at, PK(project_id,user_id))        -- M:N User-Project
grants(grant_id PK, project_id, user_id, table_name, privilege CHECK IN (SELECT,INSERT,UPDATE,DELETE),
       granted_by FK->users, UNIQUE(project_id,user_id,table_name,privilege),
       FK(project_id,user_id) -> project_members)                                    -- weak/associative
functional_deps(fd_id PK, project_id FK, table_name, lhs, rhs)
routines(routine_id PK, project_id FK, name, body, UNIQUE(project_id,name))
activity_log(log_id PK, project_id FK, user_id FK, kind, statement, logged_at)
```

Relationships: User 1-N Project (owns); User M-N Project (member, via project_members);
Project 1-N Grant, Functional_Dep, Routine, Activity_Log. All tables are in 3NF
(`grants.table_name` is an attribute of the user's database, not a dependency on another column here).
Full DDL: `db.py`. Draw the ER diagram from this list in ERDPlus/Dia for Lab 1.

Inside every user database SchemaForge also keeps `_sf_tables` (the design spec, so constraints
round-trip) and `_audit_log` (written only by triggers).

## How the interesting parts work (for the viva)

* **DDL generation** (`engine.py`): the browser sends a JSON spec; `create_table_sql` builds the
  `CREATE TABLE`. Identifiers are validated and quoted, CHECK values are structured (never raw SQL),
  so there is no SQL injection through the designer.
* **ALTER on SQLite**: SQLite cannot modify most column properties in place, so `rebuild_table`
  does the classic 12-step rebuild inside one transaction: create new table, copy rows, drop old,
  rename, recreate indexes/triggers/views, run `PRAGMA foreign_key_check`, roll back on failure.
* **Privileges**: GRANT/REVOKE rows live in `grants`; at connect time they become a SQLite
  *authorizer* callback that approves or denies each read/insert/update/delete on each table
  *as the statement is compiled*, so it also protects the SQL console. Views and triggers run with
  the owner's rights (like Oracle definer rights), which is why a user can be given a view but not its base tables.
  ON DELETE CASCADE / SET NULL actions are recognised as foreign-key actions and do not need privileges on the child table.
* **Transactions**: BEGIN opens a dedicated connection kept server-side per (user, database);
  SAVEPOINT / ROLLBACK TO / COMMIT / ROLLBACK map one-to-one to SQL. Idle transactions are rolled back after 10 minutes.
* **Normalizer** (`normalize.py`): attribute closure, candidate-key search, minimal cover, 3NF synthesis, BCNF decomposition.
* **Query builder** (`querybuilder.py`): validates every alias/column against the real schema, binds every value as a parameter.

## Honest limits

* The engine is SQLite. It has no stored procedures, cursors, MERGE or real users, so "routines" are saved SQL batches,
  privileges are enforced by the app + authorizer, and the **PL/SQL / PL/pgSQL objects are generated in the export**
  (PostgreSQL export was loaded and run on a real PostgreSQL 16 server; the Oracle and MySQL output was generated
  but **not executed** against those servers - review before submitting).
* A member who has write privileges on a parent table can also read the foreign-key column of its child tables
  (needed so SQLite can check referential integrity). They cannot read other child columns.
* Data types are enforced by the app's validator (SQLite itself is loosely typed).
* Built for lab/demo scale (single process, small data), not production hosting.
