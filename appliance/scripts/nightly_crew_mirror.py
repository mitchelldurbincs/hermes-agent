#!/usr/bin/env python3
"""Optional main-mirror maintenance in a separate checkout; never the live fork."""
import argparse
from pathlib import Path
import uuid

from nightly_crew_state import git, locked, read


def mirror(config):
    repo = Path(config['mirror_repo']).resolve()
    if repo == Path(config['repo']).resolve():
        raise ValueError('Mirror must use its own checkout')
    with locked(config['state']):
        if git(repo, 'symbolic-ref', '--short', 'HEAD') != 'main' or git(repo, 'status', '--porcelain'):
            raise ValueError('Mirror checkout must be clean on main')
        ref = f'refs/nightly/mirror/{uuid.uuid4().hex}'
        git(repo, 'fetch', '--no-tags', '--no-write-fetch-head', 'upstream', f'refs/heads/main:{ref}')
        target = git(repo, 'rev-parse', ref)
        git(repo, 'merge-base', '--is-ancestor', 'HEAD', target)
        git(repo, 'merge', '--ff-only', target)
        git(repo, 'push', 'origin', f'{target}:refs/heads/main')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    mirror(read(parser.parse_args().config))
