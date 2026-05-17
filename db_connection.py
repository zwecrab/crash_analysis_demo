"""
db_connection.py
----------------
Reusable PostgreSQL connection helper for the L-DCM Crash Risk Analysis project.
Reads credentials from .env (never hard-code them).

Dependencies:
    pip install psycopg2-binary python-dotenv
"""

import os
from dotenv import load_dotenv
import psycopg2
from psycopg2 import OperationalError

# Load variables from .env in the same directory as this script
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))


def get_connection(dbname: str = "carcrash"):
    """Return a live psycopg2 connection using .env credentials.

    Args:
        dbname: Database name to connect to. Defaults to 'carcrash'.
                Pass None to use the DB_NAME environment variable instead.
    """
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST"),
            port=int(os.getenv("DB_PORT", 5432)),
            dbname=dbname or os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            connect_timeout=10,
        )
        return conn
    except OperationalError as e:
        print(f"[db_connection] Could not connect to database: {e}")
        raise


# ---------------------------------------------------------------------------
# Quick smoke-test — run this file directly to verify the connection works:
#   python db_connection.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Testing database connection…")
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT version();")
        version = cur.fetchone()[0]
        print(f"Connected! PostgreSQL version:\n  {version}")
    conn.close()
    print("Connection closed.")
