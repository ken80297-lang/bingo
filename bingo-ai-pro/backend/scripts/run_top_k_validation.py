"""Run the isolated Top-k validation suite with credentials removed and a deadline.

Local process/RSS evidence only; this is not a Render Cron benchmark.
"""
import json
import argparse
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    'test_top_k_shadow_v3.py',
    'test_top_k_shadow_evaluation.py', 'test_top_k_shadow_roundtrip.py',
    'test_top_k_readonly_audit.py', 'test_top_k_known_gaps.py', 'test_top_k_shadow_integrity.py',
    'test_prediction_service.py', 'test_prediction_lifecycle_repair.py',
    'test_shadow_dynamic_observer.py', 'test_prediction_lifecycle_orchestrator.py',
    'test_official_verification_lifecycle.py', 'test_learning_closed_loop_regression.py',
    'test_ai_lifecycle_cron_readiness.py', 'test_ai_lifecycle_worker_api.py',
]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('full', 'smoke'), default='full')
    parser.add_argument('--timeout', type=int, default=25)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 60:
        parser.error('--timeout must be between 10 and 60 seconds')
    selected_tests = TESTS if args.profile == 'full' else TESTS[:5]
    env = dict(os.environ)
    # Do not allow pytest to load credentials from the invoking shell or .env.
    for key in list(env):
        if key.startswith(('DATABASE_', 'SUPABASE_', 'PG', 'GROQ_', 'OPENAI_')):
            env.pop(key, None)
    env.update(PYTHON_DOTENV_DISABLED='1', DATABASE_TYPE='sqlite', DATABASE_URL='',
               AI_LIFECYCLE_CRON_CORE_ONLY='true', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    started = time.monotonic()
    try:
        result = subprocess.run([sys.executable, '-m', 'pytest', *['tests/'+n for n in selected_tests],
                                 '-q', '--disable-warnings'], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=args.timeout)
        report = {'exit_code': result.returncode, 'timed_out': False,
                  'elapsed_seconds': round(time.monotonic()-started, 3),
                  'peak_child_rss_kib_linux': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                  'stdout': result.stdout, 'stderr': result.stderr,
                  'scope': 'isolated local tests, not production Cron'}
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode(errors='replace')
        report = {'exit_code': 124, 'timed_out': True, 'deadline_seconds': args.timeout,
                  'partial_stdout': output[-2000:],
                  'scope': 'isolated local tests, not production Cron'}
    report['profile'] = args.profile
    report['deadline_seconds'] = args.timeout
    print(json.dumps(report, indent=2, ensure_ascii=False))
    raise SystemExit(report['exit_code'])
