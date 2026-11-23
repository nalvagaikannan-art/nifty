def test_tests_never_use_live_database():
    from app.database import engine
    url = str(engine.url)
    assert "nifty_test_db_" in url, url
    assert "/home/ubuntu/nifty/data/analyzer.db" not in url, url
