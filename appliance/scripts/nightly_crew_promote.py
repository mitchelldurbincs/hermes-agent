#!/usr/bin/env python3
"""Consume a referee-approved exact merge; hand deployment to its separate owner."""
import argparse
from datetime import UTC, datetime
from pathlib import Path
import subprocess

from nightly_crew_preflight import planner
from nightly_crew_state import digest, git, incident, locked, night, read, stamp, write


def verified(config, receipt):
    if not isinstance(receipt, dict) or receipt.get('status') != 'approved':
        raise ValueError('no_approved_candidate')
    if receipt['repo'] != config['repo'] or receipt['branch'] != config['branch']:
        raise ValueError('wrong_repository_or_branch')
    expires = datetime.fromisoformat(receipt['expires'])
    if expires.tzinfo is None:
        raise ValueError('expiry_requires_timezone')
    if receipt['night'] != night() or expires <= datetime.now(UTC):
        raise ValueError('expired_candidate')
    if not receipt.get('referee'):
        raise ValueError('missing_referee')
    evidence = Path(receipt['artifacts']) / 'gates' / 'result.json'
    if digest(evidence) != receipt['gates_sha256'] or not read(evidence).get('all_green'):
        raise ValueError('failed_or_changed_gates')
    if digest(config['pins']) != receipt['pins_manifest_sha256']:
        raise ValueError('changed_pin_manifest')
    repo, candidate = config['repo'], receipt['candidate']
    head = git(repo, 'rev-parse', 'HEAD')
    if head != receipt['fork']:
        raise ValueError('stale_head')
    if git(repo, 'symbolic-ref', '--short', 'HEAD') != config['branch'] or git(repo, 'status', '--porcelain'):
        raise ValueError('checkout_not_ready')
    for operation in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer'):
        path = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', operation))
        if path.exists():
            raise ValueError('unfinished_git_operation')
    parents = git(repo, 'rev-list', '--parents', '-n', '1', candidate).split()
    if parents != [candidate, head, receipt['upstream']]:
        raise ValueError('candidate_parent_mismatch')
    if git(repo, 'rev-parse', candidate + '^{tree}') != receipt['candidate_tree']:
        raise ValueError('candidate_tree_mismatch')
    merge = planner(config['planner'])
    merge.verify_pins(merge.Git(repo), candidate, read(config['pins']))
    return candidate


def consume(config, *, hold_only=False):
    state = Path(config['state'])
    with locked(state):
        try:
            receipt = read(state / 'candidate.json')
            candidate = verified(config, receipt)
            if hold_only or not config.get('promotion_enabled', False):
                raise ValueError('promotion_disabled')
        except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)
            write(state / 'last-consume.json', {'status': 'hold', 'reason': reason, 'created': stamp()})
            print(f'HOLD: {reason}')
            return 0
        # Receipt validation ends here. A partial promotion is never retried silently.
        transaction = {'status': 'promoting', 'before': receipt['fork'], 'candidate': candidate,
                       'upstream': receipt['upstream'], 'created': stamp(), 'referee': receipt['referee'],
                       'deployment': 'not_requested', 'rollback': 'reviewed forward repair; never force-push'}
        write(state / 'promotion.json', transaction)
        write(state / 'candidate.json', {**receipt, 'status': 'consuming'})
        try:
            # No checkout switch, new merge, dependency install or gateway action.
            git(config['repo'], 'merge', '--ff-only', candidate)
            git(config['repo'], 'push', 'origin', f"{candidate}:refs/heads/{config['branch']}")
        except (OSError, subprocess.SubprocessError) as exc:
            transaction.update(status='failed', error=str(exc))
            incident(state, config['repo'], config['branch'], 'promotion_failure', transaction)
            write(state / 'promotion.json', transaction)
            return 1
        transaction['status'] = 'promoted_awaiting_deployment_owner'
        write(state / 'promotion.json', transaction)
        write(state / 'integrated.json', {'upstream': receipt['upstream'], 'candidate': candidate})
        write(state / 'candidate.json', {**receipt, 'status': 'consumed'})
        print(f'PROMOTED: {candidate}; deployment remains a separate action')
        return 0


def approve(config, *, referee, expires):
    """Explicit referee decision, allowed only on unchanged all-green evidence."""
    state = Path(config['state'])
    with locked(state):
        receipt = read(state / 'candidate.json')
        if receipt['status'] != 'needs_review':
            raise ValueError('Candidate is not awaiting review')
        receipt.update(status='approved', referee=referee, expires=expires)
        verified(config, receipt)
        write(state / 'candidate.json', receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--hold-only', action='store_true')
    parser.add_argument('--approve', metavar='REFEREE')
    parser.add_argument('--expires', help='UTC ISO timestamp; required for approval')
    args = parser.parse_args()
    config = read(args.config)
    try:
        if args.approve:
            if not args.expires:
                parser.error('--approve requires --expires')
            approve(config, referee=args.approve, expires=args.expires)
            return 0
        return consume(config, hold_only=args.hold_only)
    except BlockingIOError:
        print('HOLD: shared nightly lock is held')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
