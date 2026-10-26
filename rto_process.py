import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

# Dropdown fields in the New Entry form -> column in cp_database_N
OPTION_COLUMNS = {
    "facility": "return_name",
    "courier": "courier_partner",
    "sku": "product_sku_code",
    "awb": "awb",
}


def _password(name):
    # PyMySQL sends a str password as latin-1, but MySQL hashed it from the UTF-8 bytes it was set
    # with, so any non-ASCII character (e.g. "£") made a correct password fail with error 1045.
    return os.environ[name].encode("utf-8")


def get_scm_connection():
    return pymysql.connect(
        host=os.environ["SCM_DB_HOST"],
        port=int(os.environ["SCM_DB_PORT"]),
        user=os.environ["SCM_DB_USER"],
        password=_password("SCM_DB_PASSWORD"),
        database=os.environ["SCM_DB_NAME"],
        connect_timeout=60,
        charset="utf8mb4",
    )


def get_dn_connection(read_timeout=None):
    """Connection to the Delivery Note (ERP) database - a different server from SCM."""
    return pymysql.connect(
        host=os.environ["DN_DB_HOST"],
        port=int(os.environ["DN_DB_PORT"]),
        user=os.environ["DN_DB_USER"],
        password=_password("DN_DB_PASSWORD"),
        database=os.environ["DN_DB_NAME"],
        connect_timeout=60,
        read_timeout=read_timeout,
        write_timeout=read_timeout,
        charset="utf8mb4",
    )


def fetch_distinct_awbs(limit=10):
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT awb FROM cp_database_N WHERE awb IS NOT NULL LIMIT %s",
                (limit,),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def fetch_distinct_values(field):
    column = OPTION_COLUMNS[field]
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT {column} FROM cp_database_N "
                f"WHERE {column} IS NOT NULL AND {column} <> '' ORDER BY {column}"
            )
            return [str(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()


def awb_exists(awb):
    conn = get_scm_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM cp_database_N WHERE awb = %s LIMIT 1", (awb,))
            return cur.fetchone() is not None
    finally:
        conn.close()


if __name__ == "__main__":
    for awb in fetch_distinct_awbs(10):
        print(awb)
