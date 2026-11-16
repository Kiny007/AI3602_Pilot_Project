from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import http.client
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse
import urllib.error
import urllib.request
import zipfile

from .common import (PilotError, canonical, digest, file_hash, jsonl, new_directory,
                     read_json, safe_child, write_json)
from .rules import invalid_text_reason, normalized

WEBIS_URL = "https://zenodo.org/records/1043504/files/corpus-webis-tldr-17.zip?download=1"
WEBIS_MD5 = "e2fb1d5026cdb895ea640bdb134d0398"


def download_webis(destination: Path, *, restart: bool = False, progress=None) -> dict:
    """Resume partial downloads; verify complete files and preserve discarded partials."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")

    def announce(message):
        if progress:
            progress(message)

    def finish(path: Path, status: str) -> dict:
        if path == partial:
            if destination.exists():
                raise PilotError("Destination appeared during download; partial file preserved")
            partial.rename(destination)
        info = {"url": WEBIS_URL, "md5": WEBIS_MD5, "sha256": file_hash(destination),
                "bytes": destination.stat().st_size}
        write_json(destination.with_suffix(destination.suffix + ".source.json"), info)
        return {**info, "status": status}

    def preserve_partial():
        if partial.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = partial.with_name(partial.name + ".backup-" + stamp)
            # Same parent, no recursive operation or deletion; never overwrite another backup.
            if backup.exists():
                raise PilotError("Backup path already exists; retry the command")
            partial.rename(backup)
            announce(f"已保留旧下载片段：{backup.name}")

    if destination.exists():
        announce("完整文件已存在，正在校验 MD5…")
        if file_hash(destination, "md5") != WEBIS_MD5:
            raise PilotError("Existing ZIP checksum mismatch; file preserved. Use a different --output path.")
        return finish(destination, "already_complete")
    if restart:
        preserve_partial()
    elif partial.exists():
        announce("发现 .part 文件，正在检查是否已下载完整…")
        if file_hash(partial, "md5") == WEBIS_MD5:
            return finish(partial, "recovered_complete")

    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "AI3602-PrivacyPilot/0.1", "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        announce(f"尝试从 {offset / 1024 / 1024:.2f} MiB 处继续下载…")
    else:
        announce("连接下载源…")
    req = urllib.request.Request(WEBIS_URL, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            raise PilotError("Server rejected the saved range. Retry with --restart; old .part will be backed up.") from None
        raise
    with response:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise PilotError("Unexpected response compression; partial file preserved")
        length = response.headers.get("Content-Length")
        expected = int(length) if length is not None else None
        if response.status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
            if not match:
                raise PilotError("Invalid Content-Range; partial file preserved")
            begin, end, total = map(int, match.groups())
            if begin != offset or not begin <= end < total or (expected is not None and expected != end - begin + 1):
                raise PilotError("Server returned the wrong byte range; partial file preserved")
            expected = end - begin + 1
        elif response.status == 200:
            if offset:
                announce("服务器未接受续传，将保留旧片段并重新下载。")
                preserve_partial()
                offset = 0
            total = expected
        else:
            raise PilotError(f"Unexpected download HTTP status: {response.status}")
        received, last_update = 0, 0.0
        try:
            with partial.open("ab" if offset else "wb") as f:
                while block := response.read(1024 * 1024):
                    f.write(block)
                    received += len(block)
                    now = time.monotonic()
                    if now - last_update >= 5:
                        current = offset + received
                        suffix = f" / {total / 1024 / 1024:.2f} MiB ({current / total:.1%})" if total else ""
                        announce(f"已下载 {current / 1024 / 1024:.2f} MiB{suffix}")
                        last_update = now
        except (OSError, http.client.HTTPException) as e:
            raise PilotError(f"Download interrupted ({type(e).__name__}). Saved .part; rerun the same command to resume.") from None
        if expected is not None and received != expected:
            raise PilotError("Download ended early. Saved .part; rerun the same command to resume.")
        if total is not None and partial.stat().st_size != total:
            raise PilotError("Server delivered a partial range. Saved .part; rerun the same command to resume.")
    announce("下载结束，正在校验 MD5 和 SHA-256…")
    checksum = file_hash(partial, "md5")
    if checksum != WEBIS_MD5:
        raise PilotError(f"Download MD5 mismatch ({checksum}). Retry with --restart; old .part will be backed up.")
    return finish(partial, "downloaded")


def source_files(path: Path) -> tuple[Path, list[Path]]:
    if path.is_file():
        return path.parent.resolve(), [path.resolve()]
    if not path.is_dir():
        raise PilotError(f"Input does not exist: {path}")
    files = sorted(p.resolve() for p in path.rglob("*") if p.is_file() and
                   (p.suffix in (".jsonl", ".json", ".zip", ".gz") or p.name.startswith("part-")))
    if not files:
        raise PilotError("No JSONL/JSON (one object per line), gzip, ZIP or Spark part files found")
    return path.resolve(), files


@contextmanager
def text_stream(path: Path, member: str | None = None):
    if member is not None:
        with zipfile.ZipFile(path) as zf, zf.open(member) as binary:
            with io.TextIOWrapper(binary, encoding="utf-8-sig") as f:
                yield f
    elif path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8-sig") as f:
            yield f
    else:
        with path.open(encoding="utf-8-sig") as f:
            yield f


def members(path: Path) -> list[str | None]:
    if path.suffix != ".zip":
        return [None]
    with zipfile.ZipFile(path) as zf:
        result = sorted(n for n in zf.namelist() if not n.endswith("/") and
                        (n.endswith((".json", ".jsonl")) or Path(n).name.startswith("part-")) and
                        not n.startswith("__MACOSX/") and not n.endswith(".crc"))
    if not result:
        raise PilotError(f"No JSON-lines members found in {path}")
    return result


def raw_lines(path: Path, member: str | None):
    with text_stream(path, member) as f:
        for number, line in enumerate(f, 1):
            if line.strip():
                yield number, line


def reddit_link(row: dict) -> str | None:
    for field in ("permalink", "link"):
        value = row.get(field)
        if not isinstance(value, str):
            continue
        if value.startswith("/r/"):
            value = "https://www.reddit.com" + value
        url = urlparse(value)
        if url.scheme in ("https", "http") and url.hostname in ("reddit.com", "www.reddit.com", "old.reddit.com"):
            return value
    return None


def prepare(input_path: Path, output: Path, cfg: dict) -> dict:
    root, files = source_files(input_path)
    source = cfg["source"]
    new_directory(output)
    counts, inventory = Counter(), []
    seen_ids, seen_text = {}, set()
    global_index = 0
    exhausted = False
    with (output / "input.jsonl").open("w", encoding="utf-8", newline="\n") as accepted, \
         (output / "excluded.jsonl").open("w", encoding="utf-8", newline="\n") as rejected:
        for path in files:
            relative = path.relative_to(root).as_posix()
            # Full-file hash anchors even a partial scan to an immutable source file.
            entry = {"file": relative, "sha256": file_hash(path), "bytes": path.stat().st_size}
            inventory.append(entry)
            for member in members(path):
                for line_no, line in raw_lines(path, member):
                    index = global_index
                    global_index += 1
                    if index < source["start"]:
                        continue
                    if counts["scanned"] >= source["limit"]:
                        exhausted = True
                        break
                    counts["scanned"] += 1
                    locator = {"file": relative, "member": member, "line": line_no}
                    reason = None
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        row, reason = {}, "malformed_json"
                    if not isinstance(row, dict):
                        row, reason = {}, "non_object"
                    # Pre-labelled privacy corpora and synthetic rows cannot silently become real inputs.
                    if any(k in row for k in ("privacy_tag", "privacy_tags")):
                        reason = "preannotated_privacy_input"
                    if row.get("synthetic") is True and not source["synthetic"]:
                        reason = "synthetic_input"
                    text = row.get(source["text_field"])
                    reason = reason or invalid_text_reason(text)
                    community = row.get("subreddit") or ""
                    if source["subreddits"] and community not in source["subreddits"]:
                        reason = reason or "outside_subreddit_scope"
                    source_id = row.get(source["id_field"])
                    source_id = str(source_id) if source_id is not None else None
                    kind = row.get("record_type", "unknown")
                    if kind not in ("submission", "comment"):
                        kind = "unknown"
                    if not reason:
                        text_hash = digest(text)
                        id_key = (kind, source_id)
                        if source_id and id_key in seen_ids:
                            reason = "duplicate_id" if seen_ids[id_key] == text_hash else "source_id_conflict"
                        elif digest(normalized(text)) in seen_text:
                            reason = "duplicate_text"
                        else:
                            if source_id:
                                seen_ids[id_key] = text_hash
                            seen_text.add(digest(normalized(text)))
                    if reason:
                        counts[reason] += 1
                        rejected.write(canonical({"locator": locator, "source_id": source_id, "reason": reason}) + "\n")
                        continue
                    ref = {"dataset": source["dataset"], "version": source["version"],
                           "url": source["url"], "kind": source["kind"], **locator,
                           "file_sha256": entry["sha256"], "source_id": source_id,
                           "record_type": kind, "text_field": source["text_field"],
                           "id_field": source["id_field"]}
                    link = reddit_link(row)
                    record = {"record_id": digest(canonical(ref)), "text": text,
                              "text_sha256": text_hash, "source_ref": ref,
                              "subreddit": community, "link": link or source["url"],
                              "link_type": "reddit" if link else "dataset",
                              "synthetic": source["synthetic"]}
                    accepted.write(canonical(record) + "\n")
                    counts["accepted"] += 1
                if exhausted:
                    break
            if exhausted:
                break
    manifest = {"schema_version": 1, "source": source, "files": inventory,
                "counts": dict(counts), "input_sha256": file_hash(output / "input.jsonl"),
                "selection": "sorted files, sorted ZIP members, nonblank lines; zero-based start before cleaning"}
    write_json(output / "source_manifest.json", manifest)
    return manifest


def load_prepared(path: Path) -> tuple[list[dict], dict]:
    manifest = read_json(path / "source_manifest.json")
    if file_hash(path / "input.jsonl") != manifest["input_sha256"]:
        raise PilotError("Prepared input checksum mismatch")
    records = list(jsonl(path / "input.jsonl"))
    if len(records) != manifest["counts"].get("accepted", 0):
        raise PilotError("Prepared record count does not match manifest")
    seen = set()
    for row in records:
        if digest(row["text"]) != row["text_sha256"] or row["record_id"] in seen:
            raise PilotError("Prepared text was changed or record IDs are duplicated")
        seen.add(row["record_id"])
        if row["source_ref"]["dataset"] != manifest["source"]["dataset"] or \
           row["source_ref"]["version"] != manifest["source"]["version"] or \
           row["synthetic"] != manifest["source"]["synthetic"]:
            raise PilotError("Prepared provenance does not match manifest")
    return records, manifest


def verify_sources(records: list[dict], raw_root: Path) -> dict:
    """Group checks by file/member so large compressed files are scanned only once."""
    groups, errors, checked = {}, [], set()
    for row in records:
        ref = row["source_ref"]
        groups.setdefault((ref["file"], ref.get("member")), []).append(row)
    hashes = {}
    for (relative, member), group in groups.items():
        path = safe_child(raw_root, relative)
        if not path.is_file():
            errors.append({"file": relative, "error": "missing_file"})
            continue
        if relative not in hashes:
            hashes[relative] = file_hash(path)
        wanted = {}
        for row in group:
            wanted.setdefault(row["source_ref"]["line"], []).append(row)
        for line_no, line in raw_lines(path, member):
            if line_no not in wanted:
                continue
            for row in wanted.pop(line_no):
                ref = row["source_ref"]
                try:
                    original = json.loads(line)
                    source_id = original.get(ref["id_field"])
                    source_id = str(source_id) if source_id is not None else None
                    ok = (hashes[relative] == ref["file_sha256"] and source_id == ref["source_id"]
                          and original.get(ref["text_field"]) == row["text"]
                          and digest(row["text"]) == row["text_sha256"])
                except (ValueError, AttributeError, KeyError):
                    ok = False
                if ok:
                    checked.add(row["record_id"])
                else:
                    errors.append({"record_id": row["record_id"], "error": "source_mismatch"})
            if not wanted:
                break
        for missing in wanted.values():
            errors.extend({"record_id": r["record_id"], "error": "missing_line"} for r in missing)
    return {"checked": len(checked), "total": len(records), "errors": errors,
            "ok": not errors and len(checked) == len(records)}
