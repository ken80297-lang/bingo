"""Run one isolated test process, then expose only its immutable result.

No Bingo app import, prediction scheduling, external DB access or rerun endpoint.
This is a test-process smoke test on Render, not a production Cron run.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if any(os.getenv(key) for key in ('DATABASE_URL', 'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'TOP_K_TEST_POSTGRES_DSN')):
        raise SystemExit('isolated Render smoke test refuses external database configuration')
    process = subprocess.run([sys.executable, str(ROOT / 'scripts/run_top_k_validation.py'),
                              '--profile', 'smoke', '--timeout', '60'],
                             cwd=ROOT, text=True, capture_output=True, timeout=75)
    report = json.loads(process.stdout)
    report.update(git_commit=os.getenv('RENDER_GIT_COMMIT'), service_id=os.getenv('RENDER_SERVICE_ID'),
                  captured_at=datetime.now(timezone.utc).isoformat(),
                  scope='isolated test subprocess on Render; not a production Cron or OOS result')
    print(json.dumps(report), flush=True)
    if process.returncode != 0 or report.get('exit_code') != 0 or report.get('timed_out'):
        raise SystemExit(process.returncode or 1)
    result = json.dumps(report).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/', '/health', '/result'):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(result)))
            self.end_headers()
            self.wfile.write(result)

        def log_message(self, *args):
            pass

    HTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), Handler).serve_forever()


if __name__ == '__main__':
    main()
