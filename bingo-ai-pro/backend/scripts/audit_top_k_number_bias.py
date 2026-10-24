"""Local JSON-only diagnostic; no database imports, writes, workers or tuning.

Variants are retrospective ablations within each saved parent 20. They are not
new frozen predictions, independent OOS observations, or promotion evidence.
"""
import argparse
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.top_k_shadow_evaluation import evaluate_frozen_prediction, rank_candidates, snapshot_hash


MODELS = ('laowanjia', 'hotcold', 'missing', 'pattern', 'balance')


def legacy_patch_candidates(numbers):
    # Mirrors database.analysis_store._patch_numbers; callers sort the draw.
    result = []
    for number in numbers:
        for gap in (1, 2, 9, 10, 11):
            for candidate in (number - gap, number + gap):
                if 1 <= candidate <= 80 and candidate not in numbers and candidate not in result:
                    result.append(candidate)
    return result[:12]


def patch_support_scores(numbers):
    """Research-only: process all sources before truncation; circular 1..80."""
    numbers = list(numbers)
    if len(set(numbers)) != len(numbers) or any(type(n) is not int or not 1 <= n <= 80 for n in numbers):
        raise ValueError('invalid_numbers')
    present = set(numbers)
    scores = Counter()
    for number in numbers:
        neighbors = {((number + sign * gap - 1) % 80) + 1
                     for gap in (1, 2, 9, 10, 11) for sign in (-1, 1)}
        scores.update(neighbors - present)
    return dict(scores)


def order_neutral_patch_candidates(numbers, issue):
    scores = patch_support_scores(numbers)
    # Deterministic issue-specific tie break; no small-number priority.
    def tie(n):
        return sha256(f'patch-support-research-v1:{issue}:{n}'.encode()).digest()
    return sorted(scores, key=lambda n: (-scores[n], tie(n)))[:12]


def _summary(picks, results, parents):
    count = sum(len(p) for p in picks)
    scored = [(p, actual, parent) for p, actual, parent in zip(picks, results, parents) if actual is not None]
    n = len(scored)
    hits = sum(len(set(p) & set(actual)) for p, actual, _ in scored)
    baseline = sum(len(p) * len(set(parent) & set(actual)) / 20 for p, actual, parent in scored)
    return dict(snapshots=len(picks), verified=n,
                share_1_20=sum(n <= 20 for p in picks for n in p) / count if count else None,
                share_1_40=sum(n <= 40 for p in picks for n in p) / count if count else None,
                mean_hits=hits / n if n else None,
                random_80_expected=1.5,
                same_parent20_expected=baseline / n if n else None)


def audit(records, analysis=()):
    variants = {k: [] for k in ('frozen_v2', 'without_position_bonus', 'without_pattern', 'without_position_and_pattern')}
    results, parents = [], []
    rejected = Counter()
    residual_max = 0.0
    candidates = {key: [] for key in MODELS}
    counts = Counter(str(r['prediction_issue']) for r in records)
    for row in records:
        try:
            if counts[str(row['prediction_issue'])] != 1:
                raise ValueError('duplicate_target')
            shadow = dict(row['shadow'], ranking_scores={
                n: float(value) for n, value in row['shadow']['ranking_scores'].items()
            })
            if shadow.get('version') != 'top-k-shadow-v2':
                raise ValueError('unsupported_version')
            if shadow['snapshot_sha256'] != snapshot_hash(shadow):
                raise ValueError('invalid_hash')
            if shadow['target_issue'] != str(row['prediction_issue']) or shadow['source_issue'] != str(row['issue']):
                raise ValueError('issue_mismatch')
            if int(shadow['target_issue']) != int(shadow['source_issue']) + 1:
                raise ValueError('nonconsecutive_issue')
            if shadow['production_twenty'] != sorted(row['recommend_numbers']):
                raise ValueError('parent_mismatch')
            record = dict(row, fast_path_metadata={'top_k_shadow': shadow})
            evaluation = evaluate_frozen_prediction(record)
            # Validate pending snapshots too, without assigning outcome scores.
            replay = rank_candidates(row['recommend_numbers'], {int(n): v for n, v in shadow['ranking_scores'].items()})
            if replay != {k: shadow[k] for k in ('top5', 'top6')}:
                raise ValueError('ranking_replay')
            actual = None
            if evaluation['status'] == 'verified':
                if sorted(row['official_numbers'] or []) != sorted(row['winning_numbers']):
                    raise ValueError('official_mismatch')
                draw_time = datetime.fromisoformat(row['official_draw_at'])
                if not (datetime.fromisoformat(shadow['frozen_at']) < draw_time
                        and datetime.fromisoformat(row['created_at']) < draw_time
                        <= datetime.fromisoformat(row['verified_at'])):
                    raise ValueError('invalid_lifecycle_time')
                actual = row['official_numbers']
            elif evaluation['status'] != 'waiting_draw':
                raise ValueError('invalid_snapshot')
            models = row['model_scores']
            adaptive = models['production_fast_path']['adaptive_learning']
            multipliers = adaptive['multipliers']
            mature = adaptive['shadow_promotion']['number_scores']
            position, pattern, pattern_base, reconstructed = Counter(), Counter(), Counter(), Counter()
            for key in MODELS:
                model = models[key]
                multiplier = max(.5, min(1.5, float(multipliers[key])))
                base = max(1.0, float(model['confidence']) / 20) * multiplier
                seen = set()
                for rank, number in enumerate(model['candidate_numbers']):
                    if number in seen:
                        raise ValueError('duplicate_model_candidate')
                    seen.add(number)
                    bonus = max(0, 20 - rank) * .15
                    reconstructed[number] += base + bonus
                    position[number] += bonus
                    if key == 'pattern':
                        pattern[number] += base + bonus
                        pattern_base[number] += base
            for number, value in mature.items():
                reconstructed[int(number)] += float(value)
            scores = {int(n): float(s) for n, s in shadow['ranking_scores'].items()}
            residual = max(abs(scores[n] - reconstructed[n]) for n in scores)
            if residual > .000051:
                raise ValueError('score_reconstruction_mismatch')
            residual_max = max(residual_max, residual)
            altered = {
                'frozen_v2': scores,
                'without_position_bonus': {n: s-position[n] for n, s in scores.items()},
                'without_pattern': {n: s-pattern[n] for n, s in scores.items()},
                'without_position_and_pattern': {n: s-position[n]-pattern_base[n] for n, s in scores.items()},
            }
            for key, score in altered.items():
                variants[key].append(rank_candidates(row['recommend_numbers'], score)['top6'])
            for key in MODELS:
                candidates[key].extend(models[key]['candidate_numbers'])
            results.append(actual)
            parents.append(row['recommend_numbers'])
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            rejected[str(exc)] += 1
    summaries = {key: _summary(picks, results, parents) for key, picks in variants.items()}
    original = variants['frozen_v2']
    for key, picks in variants.items():
        summaries[key]['changed_selection_count'] = sum(set(a) != set(b) for a, b in zip(original, picks))
    patch, source, revised, reversed_changed, stored_matches, revised_changed = [], [], [], 0, 0, 0
    for row in analysis:
        numbers = sorted(row['numbers'])
        current = legacy_patch_candidates(numbers)
        patch.extend(current)
        source.extend(numbers)
        reversed_changed += set(current) != set(legacy_patch_candidates(list(reversed(numbers))))
        stored_matches += current == row['patch_numbers']
        proposed = order_neutral_patch_candidates(numbers, row['issue'])
        revised.extend(proposed)
        revised_changed += proposed != order_neutral_patch_candidates(list(reversed(numbers)), row['issue'])
    return dict(
        classification='retrospective_diagnostic_not_oos',
        input_snapshots=len(records), analyzed_snapshots=len(parents), rejected=dict(rejected),
        maximum_score_reconstruction_error=residual_max,
        score_tolerance_reason='mature rule contributions are persisted rounded to four decimals',
        variants=summaries,
        model_candidates={key: dict(count=len(nums), share_1_20=sum(n <= 20 for n in nums)/len(nums),
                                   share_1_40=sum(n <= 40 for n in nums)/len(nums))
                          for key, nums in candidates.items() if nums},
        patch_diagnostic=dict(rows=len(analysis), stored_matches=stored_matches,
            reverse_input_changes_selection=reversed_changed,
            patch_share_1_20=sum(n <= 20 for n in patch)/len(patch) if patch else None,
            source_share_1_20=sum(n <= 20 for n in source)/len(source) if source else None,
            research_patch_share_1_20=sum(n <= 20 for n in revised)/len(revised) if revised else None,
            research_reverse_input_changes_selection=revised_changed,
            research_status='offline_only_no_hit_rate_claim'),
        limitations=['Variants selected after observing bias; not prospective OOS.',
                     'Original parent 20 and upstream candidates are held fixed.',
                     'Candidate truncation and mature-rule ordering remain in score ablations.',
                     'Higher retrospective mean does not authorize production promotion.'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshots', type=Path)
    parser.add_argument('--analysis', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(json.loads(args.snapshots.read_text()),
                           json.loads(args.analysis.read_text()) if args.analysis else []),
                     ensure_ascii=False, indent=2))
