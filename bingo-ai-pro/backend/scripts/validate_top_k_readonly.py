"""Offline audit of exported before/after snapshots; never opens a DB or worker.

Input JSON: {plan_locked_at, oos_start_issue, pairs: [{captured_at,
target_draw_at, before: <prediction record>, after: <verified record>}]}.
Timestamps must have a timezone. The plan and captures require independent
audit provenance; this tool validates consistency, not authenticity of files.
Run: python scripts/validate_top_k_readonly.py exported_pairs.json
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from math import log, sqrt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.top_k_shadow_evaluation import evaluate_frozen_prediction


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result


def audit(data):
    rejected, accepted, seen = Counter(), [], set()
    for pair in data.get('pairs', []):
        try:
            before, after = pair['before'], pair['after']
            target = str(before['prediction_issue'])
            if target in seen:
                raise ValueError('duplicate_target')
            seen.add(target)  # reject ambiguous duplicates instead of selecting a winner
            if int(target) < int(data['oos_start_issue']):
                raise ValueError('before_oos_start')
            captured, draw = instant(pair['captured_at']), instant(pair['target_draw_at'])
            if not instant(data['plan_locked_at']) < captured < draw:
                raise ValueError('not_prospective')
            shadow = before.get('fast_path_metadata', {}).get('top_k_shadow', {})
            if shadow.get('version') != 'top-k-shadow-v2':
                raise ValueError('missing_reproducible_v2_snapshot')
            if not instant(data['plan_locked_at']) < instant(shadow['frozen_at']) <= captured:
                raise ValueError('invalid_freeze_time')
            if not instant(before['predict_time']) <= captured or not instant(before['created_at']) <= captured:
                raise ValueError('record_not_saved_before_capture')
            if before.get('winning_numbers') or before.get('prediction_status') not in ('waiting_draw', 'pending', 'predicted'):
                raise ValueError('before_already_has_result')
            for field in ('id', 'issue', 'prediction_issue', 'recommend_numbers', 'fast_path_metadata', 'model_scores'):
                if before.get(field) != after.get(field):
                    raise ValueError('frozen_record_changed')
            if instant(after['verified_at']) < draw:
                raise ValueError('verified_before_draw')
            official = pair['official']
            if str(official['issue']) != target or after.get('winning_numbers') != official['numbers']:
                raise ValueError('official_draw_mismatch')
            result = evaluate_frozen_prediction(after)
            if result['status'] != 'verified':
                raise ValueError(result.get('reason', result['status']))
            top5, top6 = result['top5'], result['top6']
            if len(top5) != 5 or len(top6) != 6:
                raise ValueError('incorrect_k')
            hits20 = len(set(after['recommend_numbers']) & set(official['numbers']))
            accepted.append({'target_issue': target, 'top5_hits': result['top5_hits'],
                             'top6_hits': result['top6_hits'], 'hits20': hits20})
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            rejected[str(exc)] += 1
    # Exclude ALL copies of duplicated targets, including any accepted first copy.
    counts = Counter(str(p.get('before', {}).get('prediction_issue')) for p in data.get('pairs', []))
    accepted = [r for r in accepted if counts[r['target_issue']] == 1]
    accepted.sort(key=lambda r: int(r['target_issue']))
    n = len(accepted)
    report = {'mode': 'offline_read_only', 'input_n': len(data.get('pairs', [])),
              'eligible_n': n, 'excluded_n': len(data.get('pairs', []))-n, 'rejected': dict(rejected),
              'status': 'descriptive_only' if n else 'not_assessable', 'rows': accepted,
              'provenance': 'requires externally retained pre-draw captures and precommitted plan',
              'claims': 'No claim of predictive improvement or profitability.'}
    for k in (5, 6):
        baseline = k / 4
        mean = sum(r[f'top{k}_hits'] for r in accepted) / n if n else None
        conditional = sum(k * r['hits20'] / 20 for r in accepted) / n if n else None
        # Simultaneous 95% Hoeffding intervals for two means (union bound).
        # Only valid with independent sampled draws; serial dependence needs
        # day/block analysis before inferential use.
        radius = k * sqrt(log(80) / (2*n)) if n else None
        report[f'top{k}'] = {'mean_hits': mean, 'random_80_baseline': baseline,
                            'delta_random_80': mean-baseline if n else None,
                            'random_within_same_20': conditional,
                            'delta_within_20': mean-conditional if n else None,
                            'simultaneous_95_interval_if_independent':
                                [max(0, mean-radius), min(k, mean+radius)] if n else None}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(json.loads(args.input.read_text())), ensure_ascii=False, indent=2))
