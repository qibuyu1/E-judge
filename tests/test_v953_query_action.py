from pathlib import Path


def test_query_button_remains_actionable_and_workspace_moves_up():
    root = Path(__file__).resolve().parents[1]
    js = (root / "frontend/query.js").read_text("utf-8")
    css = (root / "frontend/styles.css").read_text("utf-8")
    index = (root / "frontend/index.html").read_text("utf-8")
    final = css.split("v9.5.3 QUERY ACTION RECOVERY + VERTICAL BALANCE", 1)[1]

    assert 'submit.disabled = true' not in js
    assert 'submit.disabled = false' in js
    assert 'API_CONFIGURATION_REQUIRED' in js
    assert 'runtimeConfig' in js
    assert 'justify-content:flex-start!important' in final
    assert 'padding-top:clamp(72px,9vh,104px)!important' in final
    assert '.search-box button:disabled' in final
    assert '20260802.1' in index
