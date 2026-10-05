"""Local administrator command to provision a test login without exposing a password."""

import argparse
import os
from pathlib import Path

from factored_bck.security import password_hash
from factored_bck.settings import Settings
from factored_bck.store import Store


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Provision an authenticated test account for an owned customer"
    )
    p.add_argument("--username", required=True)
    p.add_argument("--customer-id", required=True)
    p.add_argument("--password-file", required=True, type=Path)
    args = p.parse_args(argv)
    os.umask(0o077)
    try:
        password = args.password_file.read_text().strip()
        if not 12 <= len(password) <= 200 or not 1 <= len(args.username) <= 100:
            raise ValueError("invalid_test_credentials")
        db = Store(Settings())
        db.initialize()
        with db.connect() as pg:
            release_id = db._current(pg, pin=True)
            customer = pg.execute(
                "SELECT c.customer_id,r.manifest->>'source_kind' AS source_kind "
                "FROM bank.customers c JOIN bank.releases r USING(release_id) "
                "WHERE c.customer_id=%s AND c.release_id=%s",
                (args.customer_id, release_id),
            ).fetchone()
            if not customer:
                raise ValueError("customer_not_in_accepted_release")
            pg.execute(
                "INSERT INTO simulator.users VALUES(%s,%s,%s,%s)",
                (
                    args.username,
                    password_hash(password),
                    args.customer_id,
                    db.source_provenance(customer["source_kind"]),
                ),
            )
        print("Test account provisioned; credential values are not printed.")
        return 0
    except Exception as exc:
        print("Provisioning failed: " + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
