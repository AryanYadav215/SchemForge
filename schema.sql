-- SchemaForge platform database (the "meta" schema).
-- This is the relational model produced from the project's ER diagram.
-- Users build their own databases through the UI; every definition they make
-- is stored in these tables, and the real tables live in one SQLite file per project.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username       VARCHAR(30)  NOT NULL UNIQUE,
    password_hash  VARCHAR(255) NOT NULL,
    created_at     TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(username) >= 3)
);

CREATE TABLE IF NOT EXISTS projects (
    project_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER      NOT NULL,
    name         VARCHAR(60)  NOT NULL,
    description  VARCHAR(300),
    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_id, name),
    FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Who a project is shared with (the GRANT / REVOKE part of the app)
CREATE TABLE IF NOT EXISTS project_members (
    project_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    role        VARCHAR(10) NOT NULL CHECK (role IN ('editor', 'viewer')),
    granted_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, user_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)    REFERENCES users(user_id)       ON DELETE CASCADE
);

-- One row per table a user designs
CREATE TABLE IF NOT EXISTS table_defs (
    table_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     INTEGER     NOT NULL,
    name           VARCHAR(30) NOT NULL,
    audit_enabled  INTEGER     NOT NULL DEFAULT 0 CHECK (audit_enabled IN (0, 1)),
    UNIQUE (project_id, name),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

-- One row per column; a foreign key is stored as ref_table_id + ref_column
CREATE TABLE IF NOT EXISTS column_defs (
    column_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id      INTEGER     NOT NULL,
    position      INTEGER     NOT NULL,
    name          VARCHAR(30) NOT NULL,
    data_type     VARCHAR(12) NOT NULL
                  CHECK (data_type IN ('INTEGER','VARCHAR','DECIMAL','DATE','TIMESTAMP','BOOLEAN','TEXT')),
    length        INTEGER,
    is_pk         INTEGER NOT NULL DEFAULT 0 CHECK (is_pk IN (0, 1)),
    is_autoinc    INTEGER NOT NULL DEFAULT 0 CHECK (is_autoinc IN (0, 1)),
    not_null      INTEGER NOT NULL DEFAULT 0 CHECK (not_null IN (0, 1)),
    is_unique     INTEGER NOT NULL DEFAULT 0 CHECK (is_unique IN (0, 1)),
    is_indexed    INTEGER NOT NULL DEFAULT 0 CHECK (is_indexed IN (0, 1)),
    default_value VARCHAR(60),
    check_expr    VARCHAR(120),
    ref_table_id  INTEGER,
    ref_column    VARCHAR(30),
    on_delete     VARCHAR(10) NOT NULL DEFAULT 'RESTRICT'
                  CHECK (on_delete IN ('RESTRICT', 'CASCADE', 'SET NULL')),
    UNIQUE (table_id, name),
    FOREIGN KEY (table_id)     REFERENCES table_defs(table_id) ON DELETE CASCADE,
    FOREIGN KEY (ref_table_id) REFERENCES table_defs(table_id)
);

-- Saved queries become real database views
CREATE TABLE IF NOT EXISTS saved_views (
    view_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER      NOT NULL,
    name        VARCHAR(30)  NOT NULL,
    select_sql  VARCHAR(4000) NOT NULL,
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, name),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

-- Who did what, in which project
CREATE TABLE IF NOT EXISTS activity_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    user_id     INTEGER,
    action      VARCHAR(30)   NOT NULL,
    detail      VARCHAR(4000),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)    REFERENCES users(user_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_owner   ON projects(owner_id);
CREATE INDEX IF NOT EXISTS idx_tables_project   ON table_defs(project_id);
CREATE INDEX IF NOT EXISTS idx_columns_table    ON column_defs(table_id);
CREATE INDEX IF NOT EXISTS idx_log_project      ON activity_log(project_id);
