import pathlib

import psycopg

_DSN_PATH = pathlib.Path(__file__).resolve().parent.parent / "connection.pg"


def get_dsn() -> str:
    return _DSN_PATH.read_text().strip()


def connect(application_name: str = "refdata_demo", autocommit: bool = False):
    dsn = get_dsn()
    return psycopg.connect(dsn, application_name=application_name, autocommit=autocommit)
