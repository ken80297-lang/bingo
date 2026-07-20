from __future__ import annotations

from pathlib import Path


def test_render_keep_awake_workflow_contains_required_health_ping():
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "render-keep-awake.yml"
    text = workflow.read_text(encoding="utf-8")

    assert 'cron: "*/5 * * * *"' in text
    assert "workflow_dispatch:" in text
    assert "https://bingo-ai-pro.onrender.com/api/health" in text
    assert "--fail" in text
    assert "--max-time 30" in text
    assert "--retry 2" in text
    assert "bingo-ai-pro-github-actions-keep-awake" in text
