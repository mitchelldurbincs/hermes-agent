#!/usr/bin/env python3
"""Claim a frozen fix lane, or deliver its single third-night escalation packet."""
import argparse
from pathlib import Path
import subprocess
import time

from nightly_crew_state import claim, locked, read, stamp, write


def dispatch(config):
    """Explicit recipient and transport; successful packets are not sent twice."""
    state = Path(config['state'])
    with locked(state):
        for packet in sorted((state / 'escalations').glob('*.json')):
            value = read(packet)
            if value.get('delivered'):
                continue
            command = config.get('escalation_command')
            if not command:
                raise ValueError('No Mitchell escalation transport configured')
            subprocess.run([*command, f"Hermes nightly HOLD: {value['family']}. "
                            f"Three unresolved nights. Incident {value['incident']}; "
                            f"evidence {value['evidence']}. No pin changes or deployment performed."],
                           check=True, timeout=60)
            value['delivered'] = stamp()
            write(packet, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--claim', metavar='INCIDENT')
    parser.add_argument('--owner', default='morning-fix-lane')
    args = parser.parse_args()
    config = read(args.config)
    if args.claim:
        with locked(config['state']):
            print(claim(config['state'], args.claim, args.owner, now=time.time()))
    else:
        dispatch(config)


if __name__ == '__main__':
    main()
