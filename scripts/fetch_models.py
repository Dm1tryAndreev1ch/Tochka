#!/usr/bin/env python3
"""Download pinned voice runtime artifacts using only the Python standard library.

No model imports, package installations, services, or .env changes. The checked-in
catalog is the trust input; the output manifest is evidence, never a trust input.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import math
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(__file__).with_name("model-download-catalog.json")
PROFILES = ("mac-metal", "portable", "llm-worker")
MANIFEST = "download-manifest.json"
LOCK = ".fetch-models.lock"
CHUNK = 1024 * 1024
SPACE_MARGIN = 64 * 1024 * 1024


def emit(message):
    print(message, flush=True)


class FetchError(Exception):
    """An actionable download/configuration failure."""


class TransferError(FetchError):
    """A transfer may be safely retried, keeping the existing final file."""


def relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise FetchError(f"Unsafe artifact path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..", "") for p in value.split("/")):
        raise FetchError(f"Unsafe artifact path: {value!r}")
    return path


def safe_path(root: Path, relative: str) -> Path:
    parts = relative_path(relative).parts
    candidate = root
    for part in parts:
        candidate /= part
        if candidate.is_symlink():
            raise FetchError(
                f"Refusing symlink at artifact destination: {candidate}. "
                "Use --model-dir REAL_DIRECTORY pointing to the physical, mounted model directory "
                "with the catalog's filenames. Model symlinks are not replaced or written through; "
                "dry-run also reports this destination as blocked."
            )
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise FetchError(f"Artifact escapes model directory: {relative}")
    return candidate


def load_catalog(path: Path = CATALOG) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or not isinstance(data["artifacts"], list):
            raise ValueError("unsupported catalog schema")
        records = data["artifacts"]
        seen = set()
        for record in records:
            name = str(relative_path(record["file"]))
            relative_path(record["source_file"])
            if name in seen:
                raise ValueError(f"duplicate artifact: {name}")
            seen.add(name)
            if not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
                raise ValueError(f"missing pinned SHA-256: {name}")
            if type(record["bytes"]) is not int or record["bytes"] <= 0:
                raise ValueError(f"invalid pinned size: {name}")
            if not record["profiles"] or not set(record["profiles"]) <= set(PROFILES):
                raise ValueError(f"invalid profile: {name}")
            if record.get("source") != "bundled":
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", record["repo"]):
                    raise ValueError(f"invalid repository: {name}")
                if not re.fullmatch(r"[0-9a-f]{40}", record["revision"]):
                    raise ValueError(f"revision must be a full commit SHA: {name}")
        return records
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FetchError(f"Invalid catalog {path}: {exc}") from exc


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified(path: Path, spec: dict) -> bool:
    return (path.is_file() and path.stat().st_size == spec["bytes"]
            and digest_file(path) == spec["sha256"])


def source_url(spec: dict) -> str:
    return (f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/"
            + urllib.parse.quote(spec["source_file"], safe="/"))


def atomic_json(path: Path, value) -> None:
    # A unique temporary file also makes output manifests safe across interruption.
    if path.is_symlink():
        raise FetchError(f"Refusing symlink at {path}")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def destination_lock(root: Path):
    path = safe_path(root, LOCK)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FetchError(
            f"Another downloader may be using {root}. Check {path}; if its process "
            "has stopped, remove that stale lock and retry. Do not run other download scripts concurrently."
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "created_at": utc_now()}, stream)
        yield
    finally:
        path.unlink(missing_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def human_size(size: int) -> str:
    return f"{size / 1024**3:.3f} GiB" if size >= 1024**3 else f"{size / 1024**2:.2f} MiB"


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise FetchError("Refusing a download redirect that downgrades HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Downloader:
    def __init__(self, root: Path, *, timeout=60.0, retries=3, opener=None,
                 sleep=time.sleep, output=emit):
        self.root = root
        self.timeout = timeout
        self.retries = retries
        self.open = opener or urllib.request.build_opener(HTTPSOnlyRedirect()).open
        self.sleep = sleep
        self.output = output

    def paths(self, spec):
        target = safe_path(self.root, spec["file"])
        part = safe_path(self.root, spec["file"] + ".part")
        metadata = safe_path(self.root, spec["file"] + ".part.json")
        return target, part, metadata

    @staticmethod
    def reset_partial(part, metadata):
        part.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)

    def resume_state(self, spec, part, metadata):
        """Resume only an intact, persisted prefix from this exact artifact/ETag."""
        try:
            state = json.loads(metadata.read_text(encoding="utf-8"))
            size = part.stat().st_size
            etag = state.get("etag", "")
            if (state["url"] == source_url(spec)
                    and state["expected_sha256"] == spec["sha256"]
                    and state["expected_bytes"] == spec["bytes"]
                    and 0 < size < spec["bytes"] and state["partial_bytes"] == size
                    and isinstance(etag, str) and etag.startswith('"') and etag.endswith('"')
                    and state["partial_sha256"] == digest_file(part)):
                return state
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def check_space(self, size):
        free = shutil.disk_usage(self.root).free
        needed = max(0, size) + SPACE_MARGIN
        if free < needed:
            raise FetchError(
                f"Insufficient free space in {self.root}: {human_size(free)} free, "
                f"{human_size(needed)} required (including 64 MiB reserve). "
                "Free disk space or choose --model-dir on a larger disk."
            )

    def fetch(self, spec):
        target, part, metadata = self.paths(spec)
        if verified(target, spec):
            self.output(f"VERIFIED cache: {spec['file']}")
            return self.record(spec, target, cached=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if part.is_file() and verified(part, spec):
            part.replace(target)
            metadata.unlink(missing_ok=True)
            return self.record(spec, target)
        if spec.get("source") == "bundled":
            source = safe_path(ROOT, spec["source_file"])
            if not verified(source, spec):
                raise FetchError(f"Bundled reference missing or changed: {source}. "
                                 "Restore the approved reference or review and update its catalog hash.")
            self.check_space(spec["bytes"])
            with source.open("rb") as src, part.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
                dst.flush()
                os.fsync(dst.fileno())
            if not verified(part, spec):
                raise FetchError(f"Copied reference failed SHA-256 verification: {spec['file']}")
            part.replace(target)
            metadata.unlink(missing_ok=True)
            self.output(f"VERIFIED bundled reference: {spec['file']}")
            return self.record(spec, target)
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                self.transfer(spec, part, metadata)
                if not verified(part, spec):
                    self.reset_partial(part, metadata)
                    raise TransferError(f"SHA-256 or size mismatch for {spec['file']}; final file preserved")
                part.replace(target)
                metadata.unlink(missing_ok=True)
                self.output(f"VERIFIED download: {spec['file']}")
                return self.record(spec, target)
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    self.reset_partial(part, metadata)
                elif exc.code not in (408, 429, 500, 502, 503, 504):
                    hint = ("Public model access was refused; check network policy and model availability."
                            if exc.code in (401, 403) else "Check the pinned repository/revision in the catalog.")
                    raise FetchError(f"HTTP {exc.code} for {spec['file']}. {hint}") from exc
                last_error = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException, TransferError) as exc:
                last_error = str(exc)
            if attempt < self.retries:
                delay = min(2 ** attempt, 8)
                self.output(f"Retry {attempt + 1}/{self.retries}: {spec['file']} ({last_error}); {delay}s")
                self.sleep(delay)
        raise FetchError(f"Could not download {spec['file']}: {last_error}. "
                         "Check connectivity/TLS trust and retry the same command; validated partial files are resumable.")

    def transfer(self, spec, part, metadata):
        state = self.resume_state(spec, part, metadata)
        offset = state["partial_bytes"] if state else 0
        if not state:
            self.reset_partial(part, metadata)
        self.check_space(spec["bytes"] - offset)
        headers = {"User-Agent": "tochka-model-fetch/1", "Accept-Encoding": "identity"}
        if offset:
            headers.update({"Range": f"bytes={offset}-", "If-Range": state["etag"]})
        request = urllib.request.Request(source_url(spec), headers=headers)
        with self.open(request, timeout=self.timeout) as response:
            status = response.status
            etag = response.headers.get("ETag", "")
            if response.headers.get("Content-Encoding", "identity") not in ("identity", ""):
                raise TransferError("Unexpected HTTP content encoding")
            if offset and status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if (not match or int(match[1]) != offset or int(match[3]) != spec["bytes"]
                        or int(match[2]) != spec["bytes"] - 1 or etag != state["etag"]):
                    self.reset_partial(part, metadata)
                    raise TransferError("Resume ETag/Content-Range mismatch; restarting safely")
            elif status == 200:
                # Servers may ignore Range or reject If-Range. This is a fresh body.
                offset = 0
                self.check_space(spec["bytes"])
            else:
                raise TransferError(f"Unexpected HTTP status {status}")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) != spec["bytes"] - offset):
                self.reset_partial(part, metadata)
                raise TransferError("HTTP Content-Length differs from pinned artifact size")
            digest = hashlib.sha256()
            if offset:
                with part.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(CHUNK), b""):
                        digest.update(chunk)
            count = offset
            last_checkpoint = offset
            last_report = time.monotonic()
            self.output(f"{'RESUME' if offset else 'DOWNLOAD'} {spec['file']}: "
                        f"{human_size(offset)} / {human_size(spec['bytes'])}")
            with part.open("ab" if offset else "wb") as stream:
                def checkpoint():
                    stream.flush()
                    os.fsync(stream.fileno())
                    atomic_json(metadata, {
                        "url": source_url(spec), "etag": etag,
                        "expected_sha256": spec["sha256"], "expected_bytes": spec["bytes"],
                        "partial_bytes": count, "partial_sha256": digest.hexdigest(),
                    })
                try:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        if count + len(chunk) > spec["bytes"]:
                            raise TransferError("HTTP body exceeds pinned artifact size")
                        stream.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                        if count - last_checkpoint >= 32 * CHUNK:
                            checkpoint()
                            last_checkpoint = count
                        now = time.monotonic()
                        if now - last_report >= 1:
                            self.output(f"  {spec['file']}: {100 * count / spec['bytes']:.1f}% "
                                        f"({human_size(count)})")
                            last_report = now
                finally:
                    # Includes Ctrl-C and recoverable network exceptions.
                    checkpoint()
            if count != spec["bytes"]:
                raise TransferError(f"Incomplete response: {count}/{spec['bytes']} bytes")

    @staticmethod
    def record(spec, path, cached=False):
        return {**spec, "path": str(path), "installed": True, "verified_at": utc_now(),
                "verification": "sha256-against-pinned-catalog", "cache_hit": cached,
                "url": source_url(spec) if spec.get("source") != "bundled" else None}


def read_manifest(root):
    path = safe_path(root, MANIFEST)
    if not path.exists():
        return {"schema_version": 1, "artifacts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or not isinstance(data["artifacts"], list):
            raise ValueError("unsupported schema")
        if any(not isinstance(r.get("file"), str) for r in data["artifacts"]):
            raise ValueError("invalid artifact record")
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise FetchError(f"Invalid download manifest {path}; preserve/move it before retrying: {exc}") from exc


def run(profile, root, *, dry_run=False, exclude_llm=False, catalog=CATALOG, timeout=60.0, retries=3,
        opener=None, sleep=time.sleep, output=emit):
    specs = [s for s in load_catalog(catalog) if profile in s["profiles"]
             and not (exclude_llm and s.get("component") == "llm")]
    if not specs:
        raise FetchError(f"No artifacts in profile: {profile}")
    root = root.expanduser().absolute()
    if platform.system() == "Darwin" and len(root.parts) >= 3 and root.parts[1] == "Volumes":
        volume = Path(*root.parts[:3])
        if not volume.is_dir() or not os.path.ismount(volume):
            raise FetchError(f"External model volume is not mounted: {volume}")
    root = root.resolve()
    total = sum(s["bytes"] for s in specs)
    output(f"Profile {profile}: {len(specs)} artifacts, {human_size(total)} total; destination {root}")
    if dry_run:
        for spec in specs:
            path = safe_path(root, spec["file"])
            state = "present, not yet verified" if path.is_file() else "missing"
            source = "bundled" if spec.get("source") == "bundled" else f"{spec['repo']}@{spec['revision']}"
            output(f"  {spec['file']} | {spec['bytes']} bytes | {state} | {source} | SHA-256 {spec['sha256']}")
        output("DRY RUN: no network requests, hashing of model weights, files, or configuration changes.")
        return 0
    root.mkdir(parents=True, exist_ok=True)
    with destination_lock(root):
        downloader = Downloader(root, timeout=timeout, retries=retries, opener=opener, sleep=sleep, output=output)
        manifest = read_manifest(root)
        pending = []
        for spec in specs:
            target, part, metadata = downloader.paths(spec)
            if spec.get("source") == "bundled" and not verified(safe_path(ROOT, spec["source_file"]), spec):
                raise FetchError(f"Bundled reference missing or changed: {spec['source_file']}; "
                                 "restore approved files or review/update the catalog before downloading weights.")
            if not verified(target, spec) and not verified(part, spec):
                resume = downloader.resume_state(spec, part, metadata) if spec.get("source") != "bundled" else None
                pending.append(spec["bytes"] - (resume["partial_bytes"] if resume else 0))
        if pending:
            downloader.check_space(sum(pending))
        for spec in specs:
            record = downloader.fetch(spec)
            manifest["artifacts"] = [r for r in manifest["artifacts"] if r["file"] != spec["file"]] + [record]
            manifest["updated_at"] = utc_now()
            atomic_json(safe_path(root, MANIFEST), manifest)
        output(f"All {len(specs)} artifacts verified. Manifest: {root / MANIFEST}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--model-dir", type=Path, default=ROOT / "models",
                        help="Model destination (created if needed); does not edit application configuration")
    parser.add_argument("--dry-run", action="store_true", help="Offline list of pinned artifacts/sizes; no writes")
    parser.add_argument("--exclude-llm", action="store_true", help="Speech files only; LLM runs on another host")
    parser.add_argument("--timeout", type=float, default=60, help="Network socket timeout in seconds (default 60)")
    parser.add_argument("--retries", type=int, default=3, help="Retries after first transfer attempt (default 3)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.retries < 0 or args.retries > 20:
        parser.error("--timeout must be positive and --retries must be between 0 and 20")
    if args.exclude_llm and args.profile == "llm-worker":
        parser.error("--exclude-llm cannot be combined with --profile llm-worker")
    try:
        return run(args.profile, args.model_dir, dry_run=args.dry_run, exclude_llm=args.exclude_llm,
                   timeout=args.timeout, retries=args.retries)
    except (FetchError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Final files preserved; rerun the same command to resume validated partials.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
