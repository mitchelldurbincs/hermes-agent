"""Receipt-only promotion and queue contracts against disposable Git repositories."""
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.platforms("linux")

SCRIPTS = Path(__file__).resolve().parents[2] / 'appliance' / 'scripts'
PLANNER = Path(__file__).resolve().parents[2] / 'scripts' / 'nightly_crew.py'


@pytest.fixture
def crew(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import nightly_crew_preflight
    import nightly_crew_promote
    import nightly_crew_state
    import nightly_crew_gates
    return nightly_crew_preflight, nightly_crew_promote, nightly_crew_state, nightly_crew_gates


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, path, text):
    (repo / path).write_text(text)
    git(repo, 'add', path)
    git(repo, 'commit', '-qm', 'fixture change')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def fixture_repo(tmp_path):
    repo, remote = tmp_path / 'repo', tmp_path / 'remote.git'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@localhost')
    base = commit(repo, 'pin', 'approved\n')
    upstream = commit(repo, 'upstream-file', 'upstream\n')
    git(tmp_path, 'clone', '-q', '--bare', str(repo), str(remote))
    git(repo, 'remote', 'add', 'origin', str(remote))
    git(repo, 'remote', 'add', 'upstream', str(remote))
    git(repo, 'checkout', '-qb', 'aspecttech', base)
    fork = commit(repo, 'overlay', 'fork\n')
    git(repo, 'push', '-q', 'origin', 'aspecttech')
    pins = tmp_path / 'pins.json'
    pins.write_text(json.dumps({'pin': hashlib.sha256(b'approved\n').hexdigest()}))
    gates = tmp_path / 'gates.py'
    gates.write_text("""import argparse,json,pathlib
p=argparse.ArgumentParser()
for key in ('candidate','baseline','output','bootstrap-python'): p.add_argument('--'+key)
a=p.parse_args(); o=pathlib.Path(a.output); o.mkdir()
(o/'result.json').write_text(json.dumps({'comparison_passed':True,'all_green':True}))
""")
    return dict(repo=str(repo), branch='aspecttech', state=str(tmp_path / 'state'),
                integrated_upstream=base, pins=str(pins), planner=str(PLANNER),
                gates=str(gates), python=sys.executable, bootstrap_python=sys.executable,
                promotion_enabled=True), fork, upstream, remote


def prepared(crew, fixture_repo):
    preflight, promote, state, _ = crew
    config, fork, _upstream, remote = fixture_repo
    assert preflight.preflight(config) == 0
    promote.approve(config, referee='fixture-referee',
                    expires=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    return config, state.read(Path(config['state']) / 'candidate.json'), fork, remote


@pytest.mark.parametrize('status', ['missing', 'hold', 'needs_review', 'consuming', 'consumed'])
def test_consumer_holds_without_approval(crew, fixture_repo, status):
    _, promote, state, _ = crew
    config, fork, _, remote = fixture_repo
    if status != 'missing':
        state.write(Path(config['state']) / 'candidate.json', {'status': status})
    assert promote.consume(config) == 0
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork
    assert git(remote, 'rev-parse', 'aspecttech') == fork
    assert not (Path(config['state']) / 'promotion.json').exists()
    assert state.read(Path(config['state']) / 'last-consume.json')['status'] == 'hold'


def test_consumer_rejects_stale_head(crew, fixture_repo):
    _, promote, state, _ = crew
    config, _receipt, fork, remote = prepared(crew, fixture_repo)
    newer = commit(Path(config['repo']), 'newer', 'a concurrent fork commit\n')
    assert promote.consume(config) == 0
    assert git(config['repo'], 'rev-parse', 'HEAD') == newer
    assert git(remote, 'rev-parse', 'aspecttech') == fork
    assert state.read(Path(config['state']) / 'last-consume.json')['reason'] == 'stale_head'


@pytest.mark.parametrize('mutation', ['gates', 'expired', 'parent', 'pins', 'tree'])
def test_consumer_rejects_changed_evidence(crew, fixture_repo, mutation):
    _, promote, state, _ = crew
    config, receipt, fork, remote = prepared(crew, fixture_repo)
    changes = {'expired': {'expires': '2000-01-01T00:00:00+00:00'},
               'parent': {'upstream': fork}, 'tree': {'candidate_tree': fork},
               'pins': {'pins_manifest_sha256': 'changed'}, 'gates': {'gates_sha256': 'changed'}}
    state.write(Path(config['state']) / 'candidate.json', {**receipt, **changes[mutation]})
    assert promote.consume(config) == 0
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork
    assert git(remote, 'rev-parse', 'aspecttech') == fork
    assert not (Path(config['state']) / 'promotion.json').exists()


def test_preflight_failed_gate_invalidates_prior_receipt(crew, fixture_repo):
    preflight, promote, state, _ = crew
    config, _, fork, remote = prepared(crew, fixture_repo)
    Path(config['gates']).write_text('raise SystemExit(1)\n')
    assert preflight.preflight(config) == 2
    assert promote.consume(config) == 0
    assert state.read(Path(config['state']) / 'candidate.json')['status'] == 'hold'
    assert len(list((Path(config['state']) / 'queue').glob('*.json'))) == 1
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork
    assert git(remote, 'rev-parse', 'aspecttech') == fork


def test_approved_consumer_promotes_exact_commit_without_deployment(crew, fixture_repo):
    _, promote, state, _ = crew
    config, receipt, _, remote = prepared(crew, fixture_repo)
    assert promote.consume(config) == 0
    assert git(config['repo'], 'rev-parse', 'HEAD') == receipt['candidate']
    assert git(remote, 'rev-parse', 'aspecttech') == receipt['candidate']
    result = state.read(Path(config['state']) / 'promotion.json')
    assert result['status'] == 'promoted_awaiting_deployment_owner'
    assert result['deployment'] == 'not_requested'
    assert promote.consume(config) == 0  # replay holds


def test_push_failure_keeps_receipt_failed_and_never_rewrites_history(crew, fixture_repo):
    _, promote, state, _ = crew
    config, receipt, fork, remote = prepared(crew, fixture_repo)
    hook = remote / 'hooks' / 'pre-receive'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o755)
    assert promote.consume(config) == 1
    assert git(config['repo'], 'rev-parse', 'HEAD') == receipt['candidate']
    assert git(remote, 'rev-parse', 'aspecttech') == fork
    assert state.read(Path(config['state']) / 'promotion.json')['status'] == 'failed'
    assert promote.consume(config) == 0


def test_queue_coalesces_nights_and_expires_worker_lease(crew, fixture_repo):
    _, _, state, _ = crew
    config, _, _, _ = fixture_repo
    args = (config['state'], config['repo'], config['branch'], 'session_retirement')
    first = state.incident(*args, 'sha1', scheduled_night='2026-11-01')
    repeated = state.incident(*args, 'sha2', scheduled_night='2026-11-01')
    assert first['id'] == repeated['id'] and repeated['nights'] == ['2026-11-01']
    state.claim(config['state'], first['id'], 'worker1', now=100, seconds=10)
    with pytest.raises(ValueError, match='lease'):
        state.claim(config['state'], first['id'], 'worker2', now=105)
    assert state.claim(config['state'], first['id'], 'worker2', now=111)['owner'] == 'worker2'
    state.incident(*args, 'sha3', scheduled_night='2026-11-02')
    third = state.incident(*args, 'sha4', scheduled_night='2026-11-03')
    assert third['status'] == 'needs_mitchell'
    state.incident(*args, 'sha5', scheduled_night='2026-11-03')
    assert len(list((Path(config['state']) / 'escalations').glob('*.json'))) == 1
    with pytest.raises(ValueError, match='escalated'):
        state.claim(config['state'], first['id'], 'worker3', now=9999)


def test_shared_lock_excludes_preflight_and_consumer(crew, fixture_repo):
    preflight, promote, state, _ = crew
    config, fork, _, _ = fixture_repo
    with state.locked(config['state']):
        with pytest.raises(BlockingIOError):
            preflight.preflight(config)
        with pytest.raises(BlockingIOError):
            promote.consume(config)
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork


def test_profile_comparison_rejects_equal_count_different_failures(crew):
    gates = crew[3]
    baseline = {'profile': {'a': 'passed', 'b': 'failure'}}
    candidate = {'profile': {'a': 'failure', 'b': 'passed'}}
    assert gates.compare(baseline, candidate) == ['profile:a:passed->failure']


def test_profile_comparison_requires_missing_cases_and_retirement_green(crew):
    gates = crew[3]
    baseline = {'profile': {'a': 'passed'}}
    candidate = {gates.RETIREMENT[0]: {'retire': 'failure'}}
    assert gates.compare(baseline, candidate)


def test_preflight_refuses_rewritten_upstream(crew, fixture_repo):
    preflight, _, state, _ = crew
    config, fork, _, remote = fixture_repo
    git(remote, 'update-ref', 'refs/heads/main', fork)
    config['integrated_upstream'] = fixture_repo[2]
    assert preflight.preflight(config) == 2
    receipt = state.read(Path(config['state']) / 'candidate.json')
    assert receipt['reason'] == 'upstream_ancestry'
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork


def test_third_night_stops_preflight_before_fetch(crew, fixture_repo):
    preflight, _, state, _ = crew
    config, fork, _, _ = fixture_repo
    for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
        state.incident(config['state'], config['repo'], config['branch'], 'baseline_pin_lag',
                       'frozen', scheduled_night=day)
    git(config['repo'], 'remote', 'remove', 'upstream')
    assert preflight.preflight(config) == 2
    assert state.read(Path(config['state']) / 'candidate.json')['reason'] == 'needs_mitchell'
    assert not (Path(config['state']) / 'runs').exists()
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork


def test_comparison_success_with_baseline_failures_cannot_be_approved(crew, fixture_repo):
    preflight, promote, state, _ = crew
    config, _, _, _ = fixture_repo
    Path(config['gates']).write_text("""import argparse,json,pathlib
p=argparse.ArgumentParser()
for key in ('candidate','baseline','output','bootstrap-python'): p.add_argument('--'+key)
a=p.parse_args(); o=pathlib.Path(a.output); o.mkdir()
(o/'result.json').write_text(json.dumps({'comparison_passed':True,'all_green':False}))
""")
    assert preflight.preflight(config) == 2
    receipt = state.read(Path(config['state']) / 'candidate.json')
    assert receipt['reason'] == 'baseline_pin_lag'
    with pytest.raises(ValueError, match='not awaiting review'):
        promote.approve(config, referee='fixture', expires='2099-01-01T00:00:00+00:00')


def test_mirror_rejects_live_checkout(crew, fixture_repo):
    import nightly_crew_mirror
    config, fork, _, _ = fixture_repo
    config['mirror_repo'] = config['repo']
    with pytest.raises(ValueError, match='own checkout'):
        nightly_crew_mirror.mirror(config)
    assert git(config['repo'], 'rev-parse', 'HEAD') == fork


def test_escalation_dispatch_coalesces_successful_delivery(crew, fixture_repo, tmp_path):
    import nightly_crew_queue
    state = crew[2]
    config, _, _, _ = fixture_repo
    output, sender = tmp_path / 'sent', tmp_path / 'sender.py'
    sender.write_text('import pathlib,sys\nwith pathlib.Path(sys.argv[1]).open("a") as f: f.write(sys.argv[2]+"\\n")\n')
    config['escalation_command'] = [sys.executable, str(sender), str(output)]
    for day in ('2026-11-01', '2026-11-02', '2026-11-03'):
        state.incident(config['state'], config['repo'], config['branch'], 'baseline_pin_lag',
                       'frozen', scheduled_night=day)
    nightly_crew_queue.dispatch(config)
    nightly_crew_queue.dispatch(config)
    assert len(output.read_text().splitlines()) == 1
    assert 'Three unresolved nights' in output.read_text()
