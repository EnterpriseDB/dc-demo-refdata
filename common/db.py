import os

import psycopg
from psycopg import sql

_DSN_ENV_VAR = "A1P_DATABASE_URL"


def get_dsn() -> str:
    dsn = os.environ.get(_DSN_ENV_VAR)
    if not dsn:
        raise RuntimeError(f"{_DSN_ENV_VAR} is not set")
    return dsn


def connect(application_name: str = "refdata_demo", autocommit: bool = False):
    dsn = get_dsn()
    return psycopg.connect(dsn, application_name=application_name, autocommit=autocommit)


def ident(qualified_name: str) -> sql.Identifier:
    """Quote a possibly schema-qualified name ("schema.table") as an identifier."""
    return sql.Identifier(*qualified_name.split("."))
