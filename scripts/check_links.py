#!/usr/bin/env python3
"""Check that every audio, artwork and video link in the catalog still works.

Each link gets a GET whose body is never read. Hosts that support ranges send
one byte; Cloudflare Pages ignores the range and leaves Content-Length off HEAD
responses, so HEAD would not give a size. Copies of the same file on different
hosts must also have the same size.

Only the Python standard library is needed. Nothing is changed or uploaded.

  python3 check_links.py <folder with catalog-manifest.json>

Exit code: 0 = all good, 1 = problems found, 2 = the catalog can't be read.
On GitHub Actions the report is also written to the run's Summary page.
"""

import argparse
import http.client
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "ConanSong-catalog/1.0"
TIMEOUT = 20
# Wait this many seconds before each retry round.
RETRY_WAITS = (10, 30, 60)
WORKERS = {"archive.org": 4}
DEFAULT_WORKERS = 8
# After this many temporary failures in a row, skip the rest of the host for
# this round, so a host that is down can't use up the job's time limit.
HOST_DOWN_AFTER = 20

# field: (label, Content-Type prefix, required on every track)
FIELDS = {
    "audioUrls": ("audio", "audio/", True),
    "artworkUrls": ("artwork", "image/", True),
    "videoUrls": ("video", "video/", False),
}

BROKEN = "🔴 All links broken"
PRIMARY = "🟡 Primary broken"
FALLBACK = "🟡 Fallback broken"
SIZE = "🟠 Size mismatch"
CATALOG = "🟡 Catalog entry"
LEVELS = (BROKEN, PRIMARY, FALLBACK, SIZE, CATALOG)


class CatalogError(Exception):
    pass


@dataclass
class Result:
    ok: bool
    note: str
    size: int | None = None
    retry: bool = False


@dataclass
class Entry:
    track: str
    title: str
    label: str
    mime: str
    urls: list


def host_of(url):
    try:
        return urllib.parse.urlsplit(url).netloc or "(no host)"
    except ValueError:
        return "(invalid link)"


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CatalogError(f"{path.name}: {error}") from error


def load_entries(folder):
    """Entries to check, plus problems visible without any request."""
    entries, problems = [], []
    manifest = read_json(folder / "catalog-manifest.json")
    try:
        for name in [group["file"] for group in manifest["groups"]]:
            for track in read_json(folder / name):
                track_id, title = track["id"], track.get("title", "")
                for field, (label, mime, required) in FIELDS.items():
                    urls = track.get(field) or []
                    if not urls and not required:
                        continue
                    entry = Entry(track_id, title, label, mime, urls)
                    if len(urls) < 2:
                        note = f"{len(urls)} link(s), expected at least 2"
                        problems.append((CATALOG, entry, note))
                    if len(set(urls)) != len(urls):
                        problems.append((CATALOG, entry, "same link listed twice"))
                    if urls:
                        entries.append(entry)
    except (KeyError, TypeError, AttributeError) as error:
        raise CatalogError(f"unexpected catalog format: {error!r}") from error
    if not entries:
        raise CatalogError("no links found")
    return entries, problems


def file_size(response):
    """Full file size, or None when the host doesn't say."""
    if response.headers.get("Content-Encoding", "identity") != "identity":
        return None
    if response.status == 206:
        # "bytes 0-0/12345"
        value = response.headers.get("Content-Range", "").rpartition("/")[2]
    else:
        value = response.headers.get("Content-Length", "")
    return int(value) if value.isdigit() else None


def check(url, mime):
    if not url.startswith("https://"):
        return Result(False, "not https")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
            "Range": "bytes=0-0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if not response.url.startswith("https://"):
                return Result(False, "redirects to a non-https link")
            if response.status not in (200, 206):
                return Result(False, f"HTTP {response.status}")
            kind = response.headers.get("Content-Type", "").split(";")[0].strip()
            if not kind.lower().startswith(mime):
                return Result(False, f"wrong type ({kind or 'none'})")
            size = file_size(response)
            if size == 0:
                return Result(False, "empty file")
            return Result(True, "OK", size)
    except urllib.error.HTTPError as error:
        error.close()
        retry = error.code in (408, 429) or error.code >= 500
        return Result(False, f"HTTP {error.code}", retry=retry)
    except http.client.InvalidURL:
        return Result(False, "invalid link")
    except (OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", None) or error
        if isinstance(reason, TimeoutError):
            return Result(False, "timeout", retry=True)
        return Result(False, f"no connection ({type(reason).__name__})", retry=True)
    except ValueError:
        return Result(False, "invalid link")


def check_host(host, jobs):
    """One round over a host's links. jobs: list of (url, mime)."""
    lock = threading.Lock()
    failures = 0

    def run(job):
        nonlocal failures
        with lock:
            if failures >= HOST_DOWN_AFTER:
                return Result(False, "not checked, host is down", retry=True)
        result = check(*job)
        with lock:
            failures = failures + 1 if result.retry else 0
        return result

    with ThreadPoolExecutor(WORKERS.get(host, DEFAULT_WORKERS)) as pool:
        return dict(zip(jobs, pool.map(run, jobs)))


def check_all(entries):
    results = {}
    # dict.fromkeys drops repeated links and keeps the catalog order.
    pending = list(dict.fromkeys((u, e.mime) for e in entries for u in e.urls))
    for wait in (0, *RETRY_WAITS):
        if wait:
            print(f"Retrying {len(pending)} link(s) in {wait}s", flush=True)
            time.sleep(wait)
        by_host = defaultdict(list)
        for job in pending:
            by_host[host_of(job[0])].append(job)
        with ThreadPoolExecutor(len(by_host)) as pool:
            for part in pool.map(check_host, by_host.keys(), by_host.values()):
                results.update(part)
        pending = [job for job in pending if results[job].retry]
        if not pending:
            break
    return results


def find_problems(entries, results):
    problems, unknown_sizes = [], 0
    for entry in entries:
        found = [results[(url, entry.mime)] for url in entry.urls]
        hosts = [host_of(url) for url in entry.urls]
        good = [result for result in found if result.ok]
        if len(good) < len(found):
            level = FALLBACK if found[0].ok else PRIMARY
            if not good:
                level = BROKEN
            note = ", ".join(f"{h}: {r.note}" for h, r in zip(hosts, found))
            problems.append((level, entry, note))
        if len(good) < 2:
            continue
        # A host that gives no size can't be compared. That isn't a failure.
        if any(result.size is None for result in good):
            unknown_sizes += 1
        if len({result.size for result in good if result.size is not None}) > 1:
            note = ", ".join(
                f"{h}: {r.size} bytes"
                for h, r in zip(hosts, found)
                if r.ok and r.size is not None
            )
            problems.append((SIZE, entry, note))
    return problems, unknown_sizes


def host_counts(results):
    counts = defaultdict(lambda: [0, 0])
    for (url, _), result in results.items():
        counts[host_of(url)][0 if result.ok else 1] += 1
    return sorted(counts.items())


def text_report(problems, results, unknown_sizes):
    lines = [f"Checked {len(results)} links"]
    for host, (good, bad) in host_counts(results):
        lines.append(f"  {host}: {good} OK, {bad} broken")
    if unknown_sizes:
        lines.append(f"  Size not compared (host gave none): {unknown_sizes}")
    if not problems:
        lines += ["", "✅ No problems found"]
    for level in LEVELS:
        rows = [(entry, note) for found, entry, note in problems if found == level]
        if rows:
            lines += ["", f"{level}: {len(rows)}"]
            lines += [f"  {e.track} {e.label} ({e.title}) - {n}" for e, n in rows]
    return "\n".join(lines)


def markdown_report(problems, results, unknown_sizes):
    def cell(value):
        text = str(value).replace("&", "&amp;").replace("<", "&lt;")
        return text.replace("|", "\\|").replace("\n", " ")

    lines = ["## Catalog link check", "", "| Host | OK | Broken |", "|---|---:|---:|"]
    for host, (good, bad) in host_counts(results):
        lines.append(f"| {cell(host)} | {good} | {bad} |")
    if unknown_sizes:
        lines += ["", f"Size not compared (host gave none): {unknown_sizes}"]
    if not problems:
        lines += ["", "✅ No problems found"]
    for level in LEVELS:
        rows = [(entry, note) for found, entry, note in problems if found == level]
        if rows:
            lines += ["", f"### {level}: {len(rows)}", ""]
            lines += ["| Track | Title | File | Details |", "|---|---|---|---|"]
            for entry, note in rows:
                cells = (entry.track, entry.title, entry.label, note)
                lines.append("| " + " | ".join(map(cell, cells)) + " |")
    return "\n".join(lines)


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def main():
    parser = argparse.ArgumentParser(description="Check every link in the catalog.")
    parser.add_argument("folder", type=Path, help="folder with catalog-manifest.json")
    folder = parser.parse_args().folder
    try:
        entries, problems = load_entries(folder)
    except CatalogError as error:
        print(f"Can't read the catalog: {error}", file=sys.stderr)
        write_summary(f"## Catalog link check\n\nCan't read the catalog: `{error}`")
        return 2
    results = check_all(entries)
    found, unknown_sizes = find_problems(entries, results)
    problems += found
    print(text_report(problems, results, unknown_sizes))
    write_summary(markdown_report(problems, results, unknown_sizes))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
