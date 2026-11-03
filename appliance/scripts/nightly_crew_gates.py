#!/usr/bin/env python3
"""Compare real test identities, not just equal failure counts, in isolated profiles."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

from nightly_crew_state import digest, write

CONTRACTS = (
    'tests/gateway/test_project_threads.py',
    'tests/plugins/platforms/test_discord_gate_isolation.py',
    'tests/gateway/test_completion_fold.py',
)
PROFILE = (
    'tests/gateway/test_background_command.py',
    'tests/gateway/test_queued_followup_processing_hooks.py',
    'tests/hermes_cli/test_shared_metrics_busy.py',
    'tests/gateway/test_local_post_delivery.py',
)
RETIREMENT = ('tests/gateway/test_local_closed_retirement.py', 'tests/gateway/test_api_idle_retirement.py')


def clean_env(home, python):
    home.mkdir(parents=True, exist_ok=True)
    return {'HOME': str(home), 'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC',
            'HERMES_HOME': str(home / 'profile'), 'HERMES_RUNTIME_DIR': str(home / 'runtime'),
            'HERMES_PYTHON': str(python), 'HERMES_TEST_FILE_RETRIES': '0'}


def test_file(repo, path, output, python):
    name = path.replace('/', '__')
    xml = output / f'{name}.xml'
    with (output / f'{name}.log').open('w') as log:
        result = subprocess.run(['bash', 'scripts/run_tests.sh', '-j', '1', path,
                                 '-pnightly_test_bootstrap', f'--junitxml={xml}'], cwd=repo,
                                env=clean_env(output / 'home', python), stdout=log,
                                stderr=subprocess.STDOUT, timeout=180, check=False)
    if result.returncode not in (0, 1) or not xml.exists():
        raise ValueError(f'Runner failed or produced no evidence: {path}')
    cases = list(ET.parse(xml).iter('testcase'))
    outcomes = {}
    for case in cases:
        name = case.attrib.get('classname', '') + '::' + case.attrib['name']
        outcomes[name] = next((tag for tag in ('failure', 'error', 'skipped')
                               if case.find(tag) is not None), 'passed')
    if not outcomes or 'error' in outcomes.values():
        raise ValueError(f'Empty collection or setup error: {path}')
    if bool(result.returncode) != ('failure' in outcomes.values()):
        raise ValueError(f'Exit code disagrees with test evidence: {path}')
    return path, outcomes


def profile(repo, paths, output, python):
    output.mkdir(parents=True, exist_ok=True)
    bootstrap = repo / 'nightly_test_bootstrap.py'
    if bootstrap.exists():
        raise ValueError('Reserved test bootstrap path already exists')
    bootstrap.write_text('import hermes_bootstrap\nimport run_agent\n')
    try:
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(test_file, repo, path, output, python) for path in paths]
            return dict(future.result() for future in futures)
    finally:
        bootstrap.unlink()


def compare(baseline, candidate):
    """Every shared test must still exist with its outcome; new tests must pass."""
    regressions = []
    for path, tests in baseline.items():
        for name, outcome in tests.items():
            current = candidate.get(path, {}).get(name)
            if current != outcome and not (outcome == 'failure' and current == 'passed'):
                regressions.append(f'{path}:{name}:{outcome}->{current}')
    for path, tests in candidate.items():
        for name, outcome in tests.items():
            if name not in baseline.get(path, {}) and outcome != 'passed':
                regressions.append(f'{path}:{name}:new->{outcome}')
            if path in (*CONTRACTS, *RETIREMENT) and outcome != 'passed':
                regressions.append(f'{path}:{name}:required->{outcome}')
    return sorted(set(regressions))


def counts(profile_result):
    values = [outcome for tests in profile_result.values() for outcome in tests.values()]
    return {kind: values.count(kind) for kind in ('passed', 'failure', 'error', 'skipped')}


def build(repo, output, bootstrap_python):
    python = output / 'venv' / 'bin' / 'python'
    with (output / 'build.log').open('w') as log:
        subprocess.run([bootstrap_python, '-m', 'pm.build_env', '--source', str(repo),
                        '--out', str(output / 'venv'), '--group', 'dev', '--group', 'test', '--extra', 'messaging'],
                       cwd=repo, env=clean_env(output / 'build-home', bootstrap_python),
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=600)
    return python


def gates(candidate, baseline, output, bootstrap_python):
    output.mkdir(parents=True, exist_ok=False)
    result = {'comparison_passed': False, 'all_green': False}
    try:
        for name in ('baseline', 'candidate'):
            (output / name).mkdir()
        base_python = build(baseline, output / 'baseline', bootstrap_python)
        candidate_python = build(candidate, output / 'candidate', bootstrap_python)
        base = profile(baseline, (*CONTRACTS, *PROFILE), output / 'baseline', base_python)
        current = profile(candidate, (*CONTRACTS, *PROFILE, *RETIREMENT), output / 'candidate', candidate_python)
        regressions = compare(base, current)
        with (output / 'checks.log').open('w') as log:
            checks = subprocess.run([str(candidate_python), 'scripts/check', '--base', 'HEAD^1'],
                                    cwd=candidate, env=clean_env(output / 'check-home', candidate_python),
                                    stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False)
        result.update(baseline=base, candidate=current, baseline_counts=counts(base),
                      candidate_counts=counts(current), regressions=regressions, check_exit=checks.returncode,
                      baseline_lock=digest(baseline / 'uv.lock'), candidate_lock=digest(candidate / 'uv.lock'),
                      comparison_passed=not regressions and checks.returncode == 0,
                      all_green=not regressions and checks.returncode == 0 and
                      all(value == 'passed' for tests in current.values() for value in tests.values()))
    except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError) as exc:
        result['error'] = str(exc)
    write(output / 'result.json', result)
    return 0 if result['comparison_passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('candidate', 'baseline', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--bootstrap-python', required=True)
    args = parser.parse_args()
    return gates(args.candidate, args.baseline, args.output, args.bootstrap_python)


if __name__ == '__main__':
    raise SystemExit(main())
