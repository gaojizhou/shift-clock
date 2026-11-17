import pytest

from app.healthcheck import validate


def _body(**queue_overrides):
    queue = {
        "enabled": True,
        "available": True,
        "workerHealthy": True,
        **queue_overrides,
    }
    return {"status": "ok", "testMode": True, "requestAuditQueue": queue}


def test_container_healthcheck_validates_semantic_queue_health():
    validate(_body(), expected_test_mode=True)

    with pytest.raises(RuntimeError, match="worker heartbeat"):
        validate(_body(workerHealthy=False), expected_test_mode=True)
    with pytest.raises(RuntimeError, match="testMode"):
        validate(_body(), expected_test_mode=False)
