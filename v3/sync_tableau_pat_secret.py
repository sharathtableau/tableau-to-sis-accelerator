"""Securely sync local Tableau PAT environment variables to Snowflake.

The credential values are passed as bound parameters: they are never printed,
written to a file, or embedded in this process's command line.
"""
import argparse
import os

import snowflake.connector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", default="wbr")
    parser.add_argument("--secret", default="WBR_DB.PUBLIC.TABLEAU_PAT_SECRET")
    args = parser.parse_args()

    username = os.environ.get("TABLEAU_PAT_NAME")
    password = os.environ.get("TABLEAU_PAT_SECRET")
    if not username or not password:
        raise SystemExit(
            "TABLEAU_PAT_NAME and TABLEAU_PAT_SECRET must both be present")

    # The secret object identifier is configuration, not user input. Keep its
    # accepted grammar deliberately narrow because identifiers cannot be bound.
    parts = args.secret.split(".")
    if len(parts) != 3 or any(not p.replace("_", "").isalnum() for p in parts):
        raise SystemExit("--secret must be an unquoted DATABASE.SCHEMA.NAME")

    conn = snowflake.connector.connect(
        connection_name=args.connection, paramstyle="qmark")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"ALTER SECRET {args.secret} SET USERNAME = ? PASSWORD = ?",
                (username, password),
            )
        print(f"Updated {args.secret} from local environment variables.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
