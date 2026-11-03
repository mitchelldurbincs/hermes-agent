#!/usr/bin/env python3
"""Freeze and rehearse one upstream-main snapshot, never mutate the live checkout."""
import argparse
import importlib.util
import tempfile
import os
import time
from pathlib import Path
import subprocess
import sys
import uuid

from nightly_crew_state import digest, git, incident, locked, night, read, stamp, write


def planner(path):
    spec = importlib.util.spec_from_file_location('crew_merge_planner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def freeze(config, run):
    repo, branch = config['repo'], config['branch']
    if git(repo, 'symbolic-ref', '--short', 'HEAD') != branch:
        raise ValueError('checkout_branch')
    if git(repo, 'status', '--porcelain'):
        raise ValueError('dirty_checkout')
    fork = git(repo, 'rev-parse', 'HEAD')
    ref = f'refs/nightly/snapshots/{run.name}'
    git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', 'upstream', f'refs/heads/main:{ref}')
    upstream = git(repo, 'rev-parse', ref)
    # The frontier advances only after promotion; never trust an unrelated mirror HEAD.
    frontier = Path(config['state']) / 'integrated.json'
    previous = read(frontier)['upstream'] if frontier.exists() else config['integrated_upstream']
    try:
        git(repo, 'merge-base', '--is-ancestor', previous, fork)
        git(repo, 'merge-base', '--is-ancestor', previous, upstream)
    except subprocess.CalledProcessError as exc:
        raise ValueError('upstream_ancestry') from exc
    write(run / 'inputs.json', {'fork': fork, 'upstream': upstream, 'previous_upstream': previous})
    return fork, upstream



def compatibility(config, run, receipt):
    """Apply the reviewed non-pinned lifecycle patch through another private index."""
    patch = config.get('compat_patch')
    if not patch:
        return
    if digest(patch) != config['compat_sha256']:
        raise ValueError('compat_patch_drift')
    merge = planner(config['planner'])
    checkout = merge.Git(config['repo'])
    with tempfile.TemporaryDirectory(dir=run) as scratch:
        checkout.env['GIT_INDEX_FILE'] = str(Path(scratch) / 'index')
        checkout.run('read-tree', receipt['candidate_tree'])
        applicable = checkout.run('apply', '--cached', '--check', patch, check=False)
        if applicable.returncode:
            checkout.run('apply', '--cached', '--reverse', '--check', patch)
            receipt['compatibility'] = 'already_present'
        else:
            checkout.run('apply', '--cached', patch)
            receipt['compatibility'] = 'applied'
        tree = checkout.run('write-tree').stdout.decode().strip()
        merge.verify_pins(checkout, tree, read(config['pins']))
    receipt['candidate_tree'] = tree
    receipt['compat_sha256'] = config['compat_sha256']
    (run / 'candidate.patch').write_bytes(checkout.run('diff', '--binary', '--full-index',
                                                     receipt['fork'], tree).stdout)
    receipt['patch_sha256'] = digest(run / 'candidate.patch')
    (run / 'fork-overlay.patch').write_bytes(checkout.run('diff', '--binary', receipt['upstream'], tree).stdout)


def validate(config, run, fork, upstream, receipt):
    """Gate the exact tree in disposable worktrees, with isolated profile/tool state."""
    repo = config['repo']
    candidate = git(repo, '-c', 'user.name=Nightly Crew', '-c', 'user.email=nightly@localhost',
                    'commit-tree', receipt['candidate_tree'], '-p', fork, '-p', upstream,
                    '-m', 'Merge frozen upstream snapshot for crew review')
    git(repo, 'update-ref', f'refs/nightly/candidates/{run.name}', candidate)
    work, base = run / 'candidate', run / 'baseline'
    git(repo, 'worktree', 'add', '--detach', str(work), candidate)
    try:
        git(repo, 'worktree', 'add', '--detach', str(base), fork)
        try:
            with (run / 'gates.log').open('w') as log:
                result = subprocess.run([config['python'], config['gates'], '--candidate', str(work),
                                         '--baseline', str(base), '--output', str(run / 'gates'),
                                         '--bootstrap-python', config['bootstrap_python']],
                                        stdout=log, stderr=subprocess.STDOUT, timeout=1200, check=False)
            gates = read(run / 'gates' / 'result.json')
            receipt.update(candidate=candidate, gates=gates, gate_exit=result.returncode,
                           gates_sha256=digest(run / 'gates' / 'result.json'))
            if result.returncode or not gates.get('comparison_passed'):
                raise ValueError('test_regression')
            if not gates.get('all_green'):
                raise ValueError('baseline_pin_lag')
        finally:
            git(repo, 'worktree', 'remove', str(base))
    finally:
        git(repo, 'worktree', 'remove', str(work))


def preflight(config):
    state = Path(config['state'])
    with locked(state):
        for path in sorted((state / 'queue').glob('*.json')):
            queued = read(path)
            if queued['repo'] != str(Path(config['repo']).resolve()) or queued['branch'] != config['branch']:
                continue
            if queued['status'] == 'needs_mitchell' or queued['lease_until'] > time.time():
                write(state / 'candidate.json', {'status': 'hold', 'reason': queued['status'],
                                                 'incident': queued['id'], 'created': stamp()})
                print(f"HOLD: incident {queued['id']} is {queued['status']}")
                return 2
        # Invalidate yesterday's pointer before any network, tests or assembly.
        write(state / 'candidate.json', {'status': 'hold', 'reason': 'preflight_running', 'created': stamp()})
        run = state / 'runs' / f'{night()}-{uuid.uuid4().hex[:12]}'
        run.mkdir(parents=True)
        receipt = {'status': 'hold', 'created': stamp(), 'night': night(), 'repo': config['repo'],
                   'branch': config['branch'], 'artifacts': str(run), 'deployable': False}
        family = 'preflight_failure'
        try:
            fork, upstream = freeze(config, run)
            receipt.update(fork=fork, upstream=upstream)
            merge = planner(config['planner'])
            pins = read(config['pins'])
            assembled = merge.assemble(merge.Git(config['repo']), fork, upstream, pins, run)
            receipt.update(assembled)
            receipt['pins_manifest_sha256'] = digest(config['pins'])
            if assembled['unresolved']:
                raise ValueError('merge_conflict:' + ','.join(assembled['unresolved']))
            compatibility(config, run, receipt)
            validate(config, run, fork, upstream, receipt)
            if git(config['repo'], 'rev-parse', 'HEAD') != fork:
                raise ValueError('stale_head')
            receipt.update(status='needs_review', reason='awaiting_referee')
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            family = str(exc) if isinstance(exc, ValueError) else family
            receipt.update(status='hold', reason=family, error=str(exc))
            queued = incident(state, config['repo'], config['branch'], family, str(run))
            receipt['incident'] = queued['id']
        write(run / 'receipt.json', receipt)
        write(state / 'candidate.json', receipt)
        print(f"{receipt['status'].upper()}: {receipt.get('reason')}; {run}")
        return 0 if receipt['status'] == 'needs_review' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--record-exit', action='store_true')
    args = parser.parse_args()
    config = read(args.config)
    if args.record_exit:
        result = os.environ.get('SERVICE_RESULT', 'success')
        if result != 'success':
            with locked(config['state']):
                evidence = {'service_result': result, 'created': stamp()}
                incident(config['state'], config['repo'], config['branch'], 'preflight_timeout_or_crash', evidence)
                write(Path(config['state']) / 'candidate.json', {'status': 'hold', **evidence})
        return 0
    try:
        return preflight(config)
    except BlockingIOError:
        print('HOLD: shared nightly lock is held')
        return 0


if __name__ == '__main__':
    sys.exit(main())
