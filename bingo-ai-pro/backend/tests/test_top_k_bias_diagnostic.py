import ast
import copy
import importlib.util
from pathlib import Path
import random
from services.top_k_shadow_evaluation import freeze_selection, snapshot_hash

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'audit_top_k_number_bias.py'
spec = importlib.util.spec_from_file_location('bias_audit', SCRIPT)
bias = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bias)


def test_legacy_reproduction_matches_actual_function():
    source = SCRIPT.parents[1] / 'database' / 'analysis_store.py'
    function = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == '_patch_numbers')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
    for seed in range(20):
        numbers = sorted(random.Random(seed).sample(range(1, 81), 20))
        assert bias.legacy_patch_candidates(numbers) == namespace['_patch_numbers'](numbers)


def test_research_candidates_ignore_input_order_without_mutation():
    numbers = random.Random(4).sample(range(1, 81), 20)
    original = copy.deepcopy(numbers)
    expected = bias.order_neutral_patch_candidates(numbers, '115059944')
    for seed in range(20):
        shuffled = random.Random(seed).sample(numbers, len(numbers))
        assert bias.order_neutral_patch_candidates(shuffled, '115059944') == expected
    assert numbers == original
    assert len(expected) == 12 and len(set(expected)) == 12
    assert not set(expected) & set(numbers)


def test_research_support_is_equivariant_under_all_circular_rotations():
    numbers = random.Random(5).sample(range(1, 81), 20)
    support = bias.patch_support_scores(numbers)
    for shift in range(80):
        rotate = lambda n: (n + shift - 1) % 80 + 1
        assert bias.patch_support_scores([rotate(n) for n in numbers]) == {rotate(n): value for n, value in support.items()}


def test_circular_neighbors_include_80_to_1():
    assert bias.patch_support_scores([80])[1] == 1
    assert bias.patch_support_scores([1])[80] == 1


def test_score_decomposition_and_pending_has_no_imputed_hits():
    numbers = list(range(1, 21))
    models = {key: {'confidence': 60, 'candidate_numbers': numbers} for key in bias.MODELS}
    models['production_fast_path'] = {'adaptive_learning': {
        'multipliers': {key: 1 for key in bias.MODELS},
        'shadow_promotion': {'number_scores': {}}}}
    scores = {n: 5 * (3 + (21 - n) * .15) for n in numbers}
    shadow = freeze_selection(numbers, scores, '100', '101')
    row = dict(issue='100', prediction_issue='101', recommend_numbers=numbers,
               shadow=shadow, model_scores=models, prediction_status='waiting_draw')
    original = copy.deepcopy(row)
    result = bias.audit([row])
    assert result['rejected'] == {}
    assert result['maximum_score_reconstruction_error'] < 1e-10
    assert result['variants']['without_position_bonus']['verified'] == 0
    assert result['variants']['without_position_bonus']['mean_hits'] is None
    assert row == original
    shadow['ranking_scores']['1'] = 2000
    shadow['snapshot_sha256'] = snapshot_hash(shadow)
    assert bias.audit([row])['analyzed_snapshots'] == 0
