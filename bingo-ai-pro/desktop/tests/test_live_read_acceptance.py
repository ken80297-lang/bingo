from desktop.core.acceptance import collect_live_read_acceptance


def test_live_repository_reads_official_prediction_analysis_rule_learning():
    result = collect_live_read_acceptance()

    assert result["official"]["valid"], result["official"]
    assert len(result["official"]["numbers"]) == 20
    assert result["official"]["super_number"] in result["official"]["numbers"]
    assert result["history"]["count"] > 1
    assert result["history"]["descending"]
    assert result["history"]["production_only"]
    if result["prediction"]["available"]:
        assert result["prediction"]["valid"], result["prediction"]
        assert len(result["prediction"]["recommend_numbers"]) == 20
        assert len(result["prediction"]["high_probability_five"]) == 5
    else:
        assert result["prediction"]["invalid_reason"] == "missing_prediction"
    assert result["analysis"]["status"] in {"finalized", "missing"}
    assert result["rule_snapshot"]["available"] in {True, False}
    assert result["learning"]["available"]
