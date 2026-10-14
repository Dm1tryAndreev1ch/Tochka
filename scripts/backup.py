"""Encrypted local backups; --restore checks the main DB in an isolated database."""

import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402
from app.secure_store import SecureStore  # noqa: E402


def run(args, **kwargs):
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--convert-legacy", action="store_true")
    args = parser.parse_args()
    private = Path(settings.private_runtime_dir)
    vault = SecureStore(private)
    backup_dir = private / "backups"
    backup_dir.mkdir(exist_ok=True, mode=0o700)
    backup_dir.chmod(0o700)
    if args.restore:
        raw = args.restore.read_bytes()
        if not raw.startswith(b"VDBK1") or len(raw) < 52:
            raise SystemExit("Expected an encrypted VDBK1 backup")
        identity = UUID(bytes=raw[5:21])
        archive = vault.open(identity, "backup", raw[21:])
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            member = bundle.getmember("voice.dump")
            if not member.isfile() or member.size > 512 * 1024 * 1024:
                raise SystemExit("Invalid database member")
            dump = bundle.extractfile(member).read()
        run(["exec", "-T", "db", "dropdb", "-U", "voice", "--if-exists", "voice_restore_test"])
        run(["exec", "-T", "db", "createdb", "-U", "voice", "voice_restore_test"])
        subprocess.run(
            [
                "docker",
                "compose",
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
            cwd=ROOT,
            input=dump,
            check=True,
            stdout=subprocess.DEVNULL,
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
                "SELECT (SELECT count(*) FROM requests) AS requests,(SELECT count(*) FROM bookings) AS bookings,(SELECT count(*) FROM outbox) AS outbox;",
            ]
        )
        print(
            "Encrypted backup authenticated; main DB restore checked in voice_restore_test. Production DB untouched."
        )
        return
    with tempfile.TemporaryDirectory(prefix="backup-", dir=private) as scratch:
        destination = Path(scratch)
        if args.convert_legacy:
            folders = [p for p in (ROOT / "backups").glob("*") if p.is_dir()]
            for folder in folders:
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
                    for path in folder.rglob("*"):
                        if path.is_file():
                            bundle.add(path, arcname=str(path.relative_to(folder)), recursive=False)
                identity = uuid4()
                output = backup_dir / (folder.name + "-legacy-" + str(identity) + ".vdbk")
                output.write_bytes(
                    b"VDBK1" + identity.bytes + vault.seal(identity, "backup", buffer.getvalue())
                )
                output.chmod(0o600)
                # Preserve the originals privately until the owner verifies legacy restore.
                quarantine = private / "legacy-backups"
                quarantine.mkdir(exist_ok=True, mode=0o700)
                folder.rename(quarantine / folder.name)
            print("Legacy backups encrypted; originals moved to private quarantine for restore verification.")
            return
        for service, user, database, name in (
            ("db", "voice", "voice", "voice.dump"),
            ("crm-db", "espocrm", "espocrm", "crm.dump"),
        ):
            with (destination / name).open("wb") as output:
                os.chmod(destination / name, 0o600)
                run(["exec", "-T", service, "pg_dump", "-U", user, "-d", database, "-Fc"], stdout=output)
        (destination / "private.env").write_bytes((ROOT / ".env").read_bytes())
        (destination / "private.env").chmod(0o600)
        with (destination / "crm-custom.tar").open("wb") as output:
            run(
                [
                    "exec",
                    "-T",
                    "crm",
                    "tar",
                    "-cf",
                    "-",
                    "custom",
                    "data/config.php",
                    "data/config-internal.php",
                ],
                stdout=output,
            )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            for path in destination.iterdir():
                bundle.add(path, arcname=path.name, recursive=False)
            for name in ("encrypted-archive", "telegram", "sip", "lan-ca"):
                directory = private / name
                if directory.is_dir():
                    bundle.add(directory, arcname="private/" + name, recursive=True)
            for name in ("lan-server-key.pem",):
                path = private / name
                if path.is_file():
                    bundle.add(path, arcname="private/" + name, recursive=False)
        if buffer.tell() > 512 * 1024 * 1024:
            raise SystemExit(
                "Backup exceeds the current 512 MiB limit; configure a streaming backup before further growth"
            )
        identity = uuid4()
        output = backup_dir / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + str(identity) + ".vdbk")
        output.write_bytes(b"VDBK1" + identity.bytes + vault.seal(identity, "backup", buffer.getvalue()))
        output.chmod(0o600)
        print("Encrypted backup:", output)
        print("Recovery requires the separate archive-master.key; it is intentionally outside this backup.")


if __name__ == "__main__":
    main()
