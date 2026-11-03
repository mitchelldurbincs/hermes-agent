"""Durable receipts, a shared lock, and coalesced nightly incidents."""
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from zoneinfo import ZoneInfo


def stamp():
    return datetime.now(UTC).isoformat()


def night():
    return datetime.now(ZoneInfo('America/New_York')).date().isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    """Replace atomically; a killed writer never leaves a partial approval pointer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


@contextmanager
def locked(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'nightly.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def git(repo, *args):
    return subprocess.run(['git', '--no-replace-objects', '-C', str(repo), *args],
                          check=True, capture_output=True, text=True, timeout=120).stdout.strip()


def incident(state, repo, branch, family, evidence, *, scheduled_night=None):
    """New SHAs and same-night retries cannot reset or artificially age an incident."""
    state = Path(state)
    key = json.dumps([str(Path(repo).resolve()), branch, family])
    ident = hashlib.sha256(key.encode()).hexdigest()[:20]
    path = state / 'queue' / f'{ident}.json'
    value = read(path) if path.exists() else {
        'id': ident, 'repo': str(Path(repo).resolve()), 'branch': branch, 'family': family,
        'first_seen': stamp(), 'nights': [], 'status': 'blocked', 'owner': None,
        'lease_until': 0, 'history': [],
    }
    day = scheduled_night or night()
    value['nights'] = sorted({*value['nights'], day})
    value['last_seen'] = stamp()
    value['evidence'] = evidence
    if not value['history'] or value['history'][-1] != evidence:
        value['history'].append(evidence)
    if len(value['nights']) >= 3:
        value['status'] = 'needs_mitchell'
        packet = state / 'escalations' / f'{ident}.json'
        if not packet.exists():
            write(packet, {'recipient': 'Mitchell', 'created': stamp(), 'incident': ident,
                           'family': family, 'evidence': evidence, 'nights': value['nights'],
                           'message': 'Three unresolved nights; automatic fix attempts stopped. HOLD.'})
    write(path, value)
    return value


def claim(state, ident, owner, *, now, seconds=900):
    """Called under the shared lock; a dead worker's lease expires without losing evidence."""
    path = Path(state) / 'queue' / f'{ident}.json'
    value = read(path)
    if value['status'] == 'needs_mitchell' or value['lease_until'] > now:
        raise ValueError('Incident escalated or lease already held')
    value.update(status='claimed', owner=owner, lease_until=now + seconds)
    write(path, value)
    return value
