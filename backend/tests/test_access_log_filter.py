import logging

from app.main import _ResearchPollingAccessFilter


def _record(path: str, status: int = 200) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "%s - \"%s %s HTTP/%s\" %s",
        ("127.0.0.1:12345", "GET", path, "1.1", status), None,
    )


def test_hides_successful_research_polling_but_keeps_errors_and_real_requests():
    filter_ = _ResearchPollingAccessFilter()
    assert filter_.filter(_record("/api/research/id/dsh/events")) is False
    assert filter_.filter(_record("/api/research/id/messages")) is False
    assert filter_.filter(_record("/api/research/6a4f9c6a-a02e-4301-be80-d4a902b7995a")) is False
    assert filter_.filter(_record("/api/research/id/dsh/events", 500)) is True
    assert filter_.filter(_record("/api/research/id/dsh/action")) is True
