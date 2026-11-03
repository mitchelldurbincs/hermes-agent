#!/usr/bin/env python3
"""Assemble a review-only upstream merge tree without updating any ref or checkout.

Inputs are immutable commit IDs and a reviewed byte-pin manifest. Outputs are a
binary patch, conflict evidence and a receipt; none constitutes deployment approval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


class Git:
    """Run Git against the selected clone, with a private index when assembling."""

    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.env = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.env.pop(key, None)

    def run(self, *args, check=True):
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(self.repo), *args],
            env=self.env, capture_output=True, check=check, timeout=120,
        )

    def commit(self, value):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("Use a full immutable 40-character commit ID")
        if self.run("cat-file", "-t", value).stdout.strip() != b"commit":
            raise ValueError(f"Not a commit: {value}")
        return value


def verify_pins(git, tree, pins):
    """Refuse drift even when a changed pin would merge without a conflict."""
    for path, digest in pins.items():
        if path.startswith("/") or ".." in Path(path).parts or "\n" in path:
            raise ValueError(f"Invalid pinned path: {path}")
        content = git.run("show", f"{tree}:{path}").stdout
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"Byte pin differs from the approved manifest: {path}")


def assemble(git, fork, upstream, pins, output):
    """Restore only explicitly pinned paths; unknown conflicts never get a tree."""
    verify_pins(git, fork, pins)
    bases = git.run("merge-base", "--all", fork, upstream).stdout.decode().splitlines()
    if len(bases) != 1:
        raise ValueError("A single merge base is required; ancestry needs review")
    merged = git.run("merge-tree", "--write-tree", "--name-only", "-z", fork, upstream, check=False)
    (output / "merge-tree.bin").write_bytes(merged.stdout)
    (output / "merge-tree.stderr").write_bytes(merged.stderr)
    if merged.returncode not in (0, 1):
        raise RuntimeError(f"merge-tree failed with exit {merged.returncode}")
    fields = merged.stdout.decode().split("\0")
    raw_tree = fields[0]
    conflicts = fields[1:fields.index("", 1)] if merged.returncode else []
    unresolved = sorted(set(conflicts) - pins.keys())
    receipt = {
        "fork": fork, "upstream": upstream, "merge_base": bases[0],
        "status": "conflicts" if unresolved else "needs_validation",
        "deployable": False, "conflicts": conflicts, "unresolved": unresolved,
        "pins_sha256": pins, "candidate_tree": None,
        "tests": "not_run", "queue_owner": "morning-fix-lane",
    }
    (output / "withheld-upstream.patch").write_bytes(git.run(
        "diff", "--binary", bases[0], upstream, "--", *pins).stdout)
    if unresolved:
        return receipt
    with tempfile.TemporaryDirectory(prefix="index-", dir=output) as scratch:
        git.env["GIT_INDEX_FILE"] = str(Path(scratch) / "index")
        try:
            git.run("read-tree", raw_tree)
            for path in pins:
                entry = git.run("ls-tree", fork, "--", path).stdout.decode()
                mode, kind, blob = entry.split("\t", 1)[0].split()
                if kind != "blob":
                    raise ValueError(f"Pin must name a blob: {path}")
                git.run("update-index", "--add", "--cacheinfo", mode, blob, path)
            tree = git.run("write-tree").stdout.decode().strip()
        finally:
            git.env.pop("GIT_INDEX_FILE", None)
    verify_pins(git, tree, pins)
    receipt["candidate_tree"] = tree
    patch = git.run("diff", "--binary", "--full-index", fork, tree).stdout
    (output / "candidate.patch").write_bytes(patch)
    receipt["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    (output / "candidate.stat").write_bytes(git.run("diff", "--stat", fork, tree).stdout)
    (output / "fork-overlay.patch").write_bytes(git.run("diff", "--binary", upstream, tree).stdout)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--fork", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory; never reused")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"status": "error", "deployable": False, "candidate_tree": None,
               "fork": args.fork, "upstream": args.upstream, "queue_owner": "morning-fix-lane"}
    try:
        git = Git(args.repo)
        fork, upstream = git.commit(args.fork), git.commit(args.upstream)
        pins = json.loads(args.pins.read_text(encoding="utf-8-sig"))
        if not isinstance(pins, dict) or not pins:
            raise ValueError("A nonempty reviewed byte-pin manifest is required")
        receipt = assemble(git, fork, upstream, pins, output)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        receipt["error"] = str(exc)
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    return {"needs_validation": 0, "conflicts": 2}.get(receipt["status"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
