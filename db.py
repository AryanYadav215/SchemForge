"""Platform database: the tables SchemaForge itself needs (users, projects,
grants, functional dependencies, saved routines).  This is the schema that is
submitted as the course ER diagram / relational model.  Every user-built
database lives in its own separate SQLite file (see engine.py)."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("SCHEMAFORGE_DATA", os.path.join(BASE, "data"))
PROJECT_DIR = os.path.join(DATA_DIR, "projects")
PLATFORM_DB = os.path.join(DATA_DIR, "platform.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    username    VARCHAR(30) NOT NULL UNIQUE CHECK (length(username) >= 3),
    pw_hash     VARCHAR(255) NOT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    project_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name        VARCHAR(60) NOT NULL,
    description VARCHAR(300) DEFAULT '',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_id, name)
);

-- a user becomes a member of somebody else's project, then receives GRANTs
CREATE TABLE IF NOT EXISTS project_members (
    project_id  INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    added_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS grants (
    grant_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    table_name  VARCHAR(60) NOT NULL,
    privilege   VARCHAR(10) NOT NULL CHECK (privilege IN ('SELECT','INSERT','UPDATE','DELETE')),
    granted_by  INTEGER NOT NULL REFERENCES users(user_id),
    UNIQUE (project_id, user_id, table_name, privilege),
    FOREIGN KEY (project_id, user_id) REFERENCES project_members(project_id, user_id)
        ON DELETE CASCADE
);

-- functional dependencies declared by the designer, used by the normalizer
CREATE TABLE IF NOT EXISTS functional_deps (
    fd_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    table_name  VARCHAR(60) NOT NULL,
    lhs         VARCHAR(200) NOT NULL,
    rhs         VARCHAR(200) NOT NULL
);

-- saved parameterised SQL ("stored procedures" for the SQLite engine)
CREATE TABLE IF NOT EXISTS routines (
    routine_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    name        VARCHAR(60) NOT NULL,
    body        TEXT NOT NULL,
    UNIQUE (project_id, name)
);

-- every DDL/DML/TCL statement the app runs on a user database
CREATE TABLE IF NOT EXISTS activity_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(user_id),
    kind        VARCHAR(10) NOT NULL,
    statement   TEXT NOT NULL,
    logged_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect():
    os.makedirs(PROJECT_DIR, exist_ok=True)
    conn = sqlite3.connect(PLATFORM_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def project_path(project_id):
    return os.path.join(PROJECT_DIR, f"project_{int(project_id)}.db")
