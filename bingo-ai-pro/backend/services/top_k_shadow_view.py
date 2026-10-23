"""Bounded public read model. Never creates predictions or modifies weights."""
from collections import Counter
from datetime import datetime, timezone

from services.top_k_shadow_evaluation import (
    evaluate_frozen_prediction, rank_candidates, snapshot_hash,
)


def _time(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def build_shadow_view(records):
    counts = Counter(str(r['prediction_issue']) for r in records)
    items, excluded = [], Counter()
    for record in records:
        try:
            target = str(record['prediction_issue'])
            if counts[target] != 1:
                raise ValueError('duplicate_target')
            stored_shadow = record['fast_path_metadata']['top_k_shadow']
            # V2 freezes every score as float. JSON transports can turn 0.0
            # into 0; restore the writer's numeric type before hash replay.
            shadow = dict(stored_shadow, ranking_scores={
                n: float(value) for n, value in stored_shadow['ranking_scores'].items()
            })
            record = dict(record, fast_path_metadata={'top_k_shadow': shadow})
            numbers = record['recommend_numbers']
            if shadow.get('version') != 'top-k-shadow-v2':
                raise ValueError('unsupported_version')
            if shadow['snapshot_sha256'] != snapshot_hash(shadow):
                raise ValueError('invalid_hash')
            if shadow['production_twenty'] != sorted(numbers):
                raise ValueError('parent_mismatch')
            source = str(record['issue'])
            if shadow['source_issue'] != source or shadow['target_issue'] != target or int(target) != int(source) + 1:
                raise ValueError('issue_mismatch')
            scores = shadow['ranking_scores']
            if set(scores) != {str(n) for n in numbers}:
                raise ValueError('score_mismatch')
            if rank_candidates(numbers, {int(n): v for n, v in scores.items()}) != {'top5': shadow['top5'], 'top6': shadow['top6']}:
                raise ValueError('ranking_mismatch')
            frozen = _time(shadow['frozen_at'])
            created = _time(record['created_at'])
            result = evaluate_frozen_prediction(record)
            official = record.get('official_numbers')
            draw_at = record.get('official_draw_at')
            if draw_at and not (frozen < _time(draw_at) and created < _time(draw_at)):
                raise ValueError('late_snapshot')
            if result['status'] not in ('waiting_draw', 'verified'):
                raise ValueError('invalid_verification')
            if result['status'] == 'verified':
                if not draw_at or record.get('official_status') != 'validated' or sorted(official or []) != sorted(record['winning_numbers']):
                    raise ValueError('official_mismatch')
                if _time(record['verified_at']) < _time(draw_at):
                    raise ValueError('early_verification')
            hits20 = len(set(numbers) & set(official)) if result['status'] == 'verified' else None
            items.append({
                'target_issue': target, 'source_issue': source,
                'frozen_at': shadow['frozen_at'], 'status': result['status'],
                'top5': shadow['top5'], 'top6': shadow['top6'],
                'top5_hits': result.get('top5_hits'), 'top6_hits': result.get('top6_hits'),
                'matched_numbers': sorted(set(shadow['top6']) & set(official)) if hits20 is not None else [],
                'parent20_hits': hits20,
            })
        except (ValueError, TypeError, KeyError, OverflowError):
            # Public response exposes neither raw scores nor exception details.
            excluded['invalid_or_unverifiable'] += 1
    verified = [r for r in items if r['status'] == 'verified']
    n = len(verified)
    mean = lambda key: sum(r[key] for r in verified) / n if n else None
    parent_mean = mean('parent20_hits')
    return {
        'status': 'ok', 'as_of': datetime.now(timezone.utc).isoformat(),
        'scope': 'latest_200_snapshots', 'evidence': 'descriptive_only',
        'scanned': len(records), 'excluded': sum(excluded.values()),
        'verified_count': n, 'waiting_count': len(items) - n,
        'top5_mean': mean('top5_hits'), 'top6_mean': mean('top6_hits'),
        'random_top5_expected': 1.25, 'random_top6_expected': 1.50,
        'same20_top5_expected': parent_mean * .25 if n else None,
        'same20_top6_expected': parent_mean * .30 if n else None,
        'items': items,
    }


def load_shadow_view():
    # No schema initialization, SQLite fallback, enrichment or learning workers.
    from database.postgres import dashboard_read_connection
    with dashboard_read_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute("SET LOCAL statement_timeout = '2000ms'")
                cur.execute('''
                    SELECT p.issue, p.prediction_issue, p.created_at,
                           p.recommend_numbers, p.fast_path_metadata,
                           p.prediction_status, p.verified_issue, p.verified_at,
                           p.winning_numbers, d.numbers AS official_numbers,
                           d.draw_time AS official_draw_at,
                           d.verification_status AS official_status
                    FROM (
                        SELECT issue, prediction_issue, created_at,
                               recommend_numbers, fast_path_metadata,
                               prediction_status, verified_issue, verified_at, winning_numbers
                        FROM prediction_history
                        WHERE fast_path_metadata ? 'top_k_shadow'
                        ORDER BY created_at DESC, id DESC LIMIT 200
                    ) p
                    LEFT JOIN official_draw_history d ON d.issue = p.prediction_issue
                    ORDER BY p.created_at DESC
                ''')
                names = [column.name for column in cur.description]
                records = [dict(zip(names, row)) for row in cur.fetchall()]
    return build_shadow_view(records)
