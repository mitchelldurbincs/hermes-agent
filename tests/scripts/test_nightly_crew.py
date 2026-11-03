"""Exercise the review-only merge CLI against real disposable Git histories."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "nightly_crew.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


def commit(repo, text):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "-qm", text)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "pin.txt").write_text("approved\n")
    (repo / "overlay.txt").write_text("base\n")
    base = commit(repo, "base")
    (repo / "overlay.txt").write_text("fork overlay\n")
    fork = commit(repo, "fork")
    git(repo, "checkout", "-q", "--detach", base)
    (repo / "pin.txt").write_text("new upstream pin\n")
    (repo / "upstream.txt").write_text("new capability\n")
    upstream = commit(repo, "upstream")
    git(repo, "checkout", "-q", "--detach", fork)
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"pin.txt": hashlib.sha256(b"approved\n").hexdigest()}))
    return repo, fork, upstream, pins


def run_plan(history, output):
    repo, fork, upstream, pins = history
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo),
        "--fork", fork, "--upstream", upstream, "--pins", str(pins), "--output", str(output)],
        capture_output=True, text=True, check=False)
    return result, json.loads((output / "receipt.json").read_text())


def test_assemble_preserves_cleanly_changed_pin_and_caller_state(history, tmp_path):
    repo, fork, _, _ = history
    (repo / "uncommitted.txt").write_text("keep my staged work\n")
    git(repo, "add", "uncommitted.txt")
    before = git(repo, "write-tree")

    result, receipt = run_plan(history, tmp_path / "output")

    assert result.returncode == 0, result.stderr
    tree = receipt["candidate_tree"]
    assert git(repo, "show", f"{tree}:pin.txt") == "approved"
    assert git(repo, "show", f"{tree}:overlay.txt") == "fork overlay"
    assert git(repo, "show", f"{tree}:upstream.txt") == "new capability"
    assert git(repo, "rev-parse", "HEAD") == fork
    assert git(repo, "write-tree") == before
    assert (repo / "uncommitted.txt").read_text() == "keep my staged work\n"
    assert receipt["deployable"] is False


def test_unknown_conflict_is_queued_without_a_candidate(history, tmp_path):
    repo, fork, upstream, pins = history
    git(repo, "checkout", "-q", "--detach", upstream)
    (repo / "overlay.txt").write_text("upstream changed the same line\n")
    upstream = commit(repo, "conflict")
    git(repo, "checkout", "-q", "--detach", fork)

    result, receipt = run_plan((repo, fork, upstream, pins), tmp_path / "output")

    assert result.returncode == 2
    assert receipt["unresolved"] == ["overlay.txt"]
    assert receipt["candidate_tree"] is None
    assert not (tmp_path / "output" / "candidate.patch").exists()
    assert git(repo, "rev-parse", "HEAD") == fork


def test_changed_pin_refuses_even_without_a_merge_conflict(history, tmp_path):
    repo, _, upstream, pins = history
    (repo / "pin.txt").write_text("unapproved change\n")
    fork = commit(repo, "pin drift")

    result, receipt = run_plan((repo, fork, upstream, pins), tmp_path / "output")

    assert result.returncode == 3
    assert "pin.txt" in receipt["error"]
    assert receipt["candidate_tree"] is None
    assert receipt["deployable"] is False
