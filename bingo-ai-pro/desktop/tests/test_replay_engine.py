from desktop.core.replay_engine import ReplayEngine


class FakeRepository:
    def get_prediction_history(self, limit=100):
        return [
            {
                "issue": "114000001",
                "prediction_issue": "114000002",
                "recommend_numbers": list(range(1, 21)),
                "super_number": 5,
                "strategy": "prod",
            }
        ]

    def get_draw_by_issue(self, issue):
        numbers = list(range(11, 31)) if issue == "114000002" else list(range(1, 21))
        return {"issue": issue, "numbers": numbers, "super_number": 5, "source": "taiwan_lottery", "verified": True}

    def get_rule_snapshots_for_issue(self, issue):
        return [{"source_issue": issue, "target_issue": "114000002", "snapshot_json": {"rules": []}}]


def test_replay_pairs_source_prediction_and_target_draw():
    engine = ReplayEngine(FakeRepository())
    records = engine.load()

    assert len(records) == 1
    assert records[0].prediction.source_issue == "114000001"
    assert records[0].target_draw.issue == "114000002"
    assert records[0].hit_numbers == list(range(11, 21))
    assert records[0].total == 1

