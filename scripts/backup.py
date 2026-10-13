"""Private, local backup including CRM; restore with --restore into an isolated test DB."""

import argparse
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(args, **kwargs):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", type=Path)
    args = parser.parse_args()
    if args.restore:
        # Never overwrite the live database by accident.
        run(["exec", "-T", "db", "dropdb", "-U", "voice", "--if-exists", "voice_restore_test"])
        run(["exec", "-T", "db", "createdb", "-U", "voice", "voice_restore_test"])
        with args.restore.open("rb") as file:
            run(
                [
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "-U",
                    "voice",
                    "-d",
                    "voice_restore_test",
                    "--no-owner",
                    "--exit-on-error",
                ],
                stdin=file,
            )
        run(
            [
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "voice",
                "-d",
                "voice_restore_test",
                "-c",
                "SELECT (SELECT count(*) FROM requests) AS requests, (SELECT count(*) FROM bookings) AS bookings, (SELECT count(*) FROM outbox) AS outbox;",
            ]
        )
        print("Restore verified in voice_restore_test; live database untouched")
        return
    destination = ROOT / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination.mkdir(parents=True, mode=0o700)
    os.chmod(destination, 0o700)
    for service, user, database, filename in [
        ("db", "voice", "voice", "voice.dump"),
        ("crm-db", "espocrm", "espocrm", "crm.dump"),
    ]:
        with (destination / filename).open("wb") as output:
            run(["exec", "-T", service, "pg_dump", "-U", user, "-d", database, "-Fc"], stdout=output)
        (destination / filename).chmod(0o600)
    config = destination / "private.env"
    config.write_bytes((ROOT / ".env").read_bytes())
    config.chmod(0o600)
    with (destination / "crm-custom.tar").open("wb") as output:
        run(
            ["exec", "-T", "crm", "tar", "-cf", "-", "custom", "data/config.php", "data/config-internal.php"],
            stdout=output,
        )
    print(f"Backup: {destination}; contains personal data and secrets, keep private")


if __name__ == "__main__":
    main()
