from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_query_and_report_are_separate_pages():
    index = (ROOT / "frontend/index.html").read_text("utf-8")
    report = (ROOT / "frontend/report.html").read_text("utf-8")
    query_js = (ROOT / "frontend/query.js").read_text("utf-8")
    report_js = (ROOT / "frontend/report.js").read_text("utf-8")
    assert 'id="reportArea"' not in index
    assert 'id="reportArea"' in report
    assert '/report?report_id=' in query_js
    assert '/api/history' in query_js and '/api/history' in report_js
    assert 'result-enter' in report
    assert 'animateRiskGauge' in report_js
    assert 'trend-animated' in report_js

def test_backend_exposes_report_page_and_history():
    main = (ROOT / "web_app/main.py").read_text("utf-8")
    cache = (ROOT / "web_app/cache.py").read_text("utf-8")
    assert '@app.get("/report")' in main
    assert '@app.get("/api/history")' in main
    assert 'CREATE TABLE IF NOT EXISTS report_history' in cache
