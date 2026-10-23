"""Prospective paired research ranking; never used by production selection."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite

from services.top_k_shadow_evaluation import rank_candidates, snapshot_hash

VERSION = 'top-k-shadow-v3'
PROTOCOL = 'patch-support-v3-20261023'
MODELS = ('laowanjia', 'hotcold', 'missing', 'pattern', 'balance')


def patch_candidates(numbers, issue):
    present = set(numbers)
    if len(numbers) != 20 or len(present) != 20 or any(type(n) is not int or not 1 <= n <= 80 for n in numbers):
        raise ValueError('invalid source draw')
    support = Counter()
    for number in numbers:
        support.update({(number + sign * gap - 1) % 80 + 1
                        for gap in (1, 2, 9, 10, 11) for sign in (-1, 1)} - present)
    tie = lambda n: sha256(f'patch-support-research-v1:{issue}:{n}'.encode()).digest()
    return sorted(support, key=lambda n: (-support[n], tie(n)))[:12]


def rank_v3(parent, scores, source, target):
    rank_candidates(parent, scores)  # Enforce the same 20-number contract.
    tie = lambda n: sha256(f'{PROTOCOL}:{source}:{target}:{n}'.encode()).digest()
    ordered = sorted(parent, key=lambda n: (-float(scores[n]), tie(n)))
    return {'top5': ordered[:5], 'top6': ordered[:6]}


def scores_from_evidence(evidence, parent):
    if set(evidence) != set(MODELS):
        raise ValueError('missing model provenance')
    scores = {n: 0.0 for n in parent}
    for model in evidence.values():
        candidates = model['candidate_numbers']
        if len(candidates) != 20 or len(set(candidates)) != 20 or any(type(n) is not int or not 1 <= n <= 80 for n in candidates):
            raise ValueError('invalid model candidates')
        confidence, multiplier = float(model['confidence']), float(model['multiplier'])
        if not (isfinite(confidence) and 1 <= confidence <= 100 and isfinite(multiplier) and .5 <= multiplier <= 1.5):
            raise ValueError('invalid model provenance')
        base = max(1.0, confidence / 20.0) * multiplier
        for n in set(candidates) & set(parent):
            scores[n] += base
    return scores


def freeze_v3(parent, history, multipliers, source, target):
    """Use already-loaded pre-target history; bounded CPU only, no extra IO."""
    from services.model_engine import run_all_models
    source, target = str(source), str(target)
    if not (source.isdecimal() and target.isdecimal() and int(target) == int(source) + 1):
        raise ValueError('invalid issue pair')
    if not isinstance(history, list) or len(history) != 100:
        raise ValueError('requires existing 100-row prediction-time history')
    issues = [str(row['issue']) for row in history]
    if len(set(issues)) != 100 or any(len(i) != 9 or i.startswith('99') or not i.isdecimal() or int(i) > int(source) for i in issues) or max(issues, key=int) != source:
        raise ValueError('history cutoff mismatch')
    original = sorted(deepcopy(history), key=lambda row: int(row['issue']), reverse=True)
    corrected = deepcopy(original)
    for row in corrected:
        row['patch_numbers'] = patch_candidates(row['numbers'], row['issue'])
    models = run_all_models(100, draws=corrected)['models']
    evidence = {m['model']: {'candidate_numbers': m['candidate_numbers'],
                             'confidence': float(m['confidence']),
                             'multiplier': float(multipliers[m['model']])} for m in models}
    scores = scores_from_evidence(evidence, parent)
    snapshot = {
        **rank_v3(parent, scores, source, target), 'version': VERSION,
        'protocol': PROTOCOL, 'source_issue': source, 'target_issue': target,
        'production_twenty': sorted(parent),
        'ranking_scores': {str(n): float(scores[n]) for n in sorted(parent)},
        'model_evidence': evidence, 'history_count': 100,
        'history_first_issue': min(issues, key=int), 'history_last_issue': source,
        'history_sha256': sha256(json.dumps(original, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        'frozen_at': datetime.now(timezone.utc).isoformat(), 'status': 'waiting_draw',
    }
    snapshot['snapshot_sha256'] = snapshot_hash(snapshot)
    return snapshot


def validate_v3(snapshot, parent, source, target):
    # Writer and public reader both replay the full frozen evidence.
    snapshot = deepcopy(snapshot)
    snapshot['ranking_scores'] = {n: float(v) for n, v in snapshot['ranking_scores'].items()}
    for model in snapshot['model_evidence'].values():
        model['confidence'] = float(model['confidence'])
        model['multiplier'] = float(model['multiplier'])
    if snapshot['version'] != VERSION or snapshot['protocol'] != PROTOCOL:
        raise ValueError('unsupported V3 snapshot')
    if (snapshot['source_issue'] != str(source) or snapshot['target_issue'] != str(target)
        or int(target) != int(source) + 1 or snapshot['production_twenty'] != sorted(parent)
        or snapshot['history_count'] != 100 or snapshot['history_last_issue'] != str(source)
        or int(snapshot['history_first_issue']) > int(source)
        or len(snapshot['history_sha256']) != 64
        or datetime.fromisoformat(snapshot['frozen_at']).tzinfo is None
        or snapshot['snapshot_sha256'] != snapshot_hash(snapshot)):
        raise ValueError('V3 provenance mismatch')
    scores = scores_from_evidence(snapshot['model_evidence'], parent)
    saved = snapshot['ranking_scores']
    if set(saved) != {str(n) for n in parent} or any(not isfinite(float(saved[str(n)])) or abs(scores[n] - float(saved[str(n)])) > 1e-9 for n in parent):
        raise ValueError('V3 score mismatch')
    expected = rank_v3(parent, scores, str(source), str(target))
    if any(snapshot[k] != expected[k] for k in ('top5', 'top6')):
        raise ValueError('V3 selection mismatch')
    return True
