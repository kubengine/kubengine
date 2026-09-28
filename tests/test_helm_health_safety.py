"""Pod health checks stay conservative when resources are absent."""

from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from core.http_api_client import helm_resource_check as health


def pod(phase="Running", *, ready=True, job=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="test-pod",
            owner_references=[SimpleNamespace(kind="Job")] if job else [],
        ),
        status=SimpleNamespace(
            phase=phase,
            conditions=[
                SimpleNamespace(
                    type="Ready", status="True" if ready else "False"
                )
            ],
            container_statuses=[],
        ),
    )


@pytest.fixture
def make_checker(monkeypatch):
    monkeypatch.setattr(health, "MAX_POLL_TIMES", 3)
    sleeps = []
    monkeypatch.setattr(health.time, "sleep", sleeps.append)

    def make(snapshots):
        calls = []
        checker = object.__new__(health.HelmResourceChecker)
        checker.namespace = "apps"
        checker.helm_label_selector = "app.kubernetes.io/instance=test"

        def list_pods(**kwargs):
            calls.append(kwargs)
            result = snapshots[min(len(calls) - 1, len(snapshots) - 1)]
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(items=result)

        checker.core_api = SimpleNamespace(list_namespaced_pod=list_pods)
        return checker, calls, sleeps

    return make


def test_empty_release_remains_unverified_after_polling(make_checker):
    checker, calls, sleeps = make_checker([[]])
    result = checker.check_pods_with_polling()
    assert result["status"] is False
    assert result["reason"] == "no_matching_pods"
    assert "Job Pod 已清理" in result["details"][0]
    assert len(calls) == 3 and len(sleeps) == 2


def test_pods_created_after_initial_empty_response_can_be_healthy(
    make_checker,
):
    checker, calls, sleeps = make_checker([[], [pod()]])
    result = checker.check_pods_with_polling()
    assert result["status"] is True
    assert len(calls) == 2 and len(sleeps) == 1


def test_completed_job_pod_is_an_explicit_success(make_checker):
    checker, calls, sleeps = make_checker(
        [[pod("Succeeded", ready=False, job=True)]]
    )
    assert checker.check_pods_with_polling()["status"] is True
    assert len(calls) == 1 and not sleeps


def test_disappeared_pending_pods_do_not_turn_the_release_healthy(
    make_checker,
):
    checker, _, _ = make_checker([[pod("Pending", ready=False)], []])
    result = checker.check_pods_with_polling()
    assert result["status"] is False
    assert result["reason"] == "no_matching_pods"


def test_kubernetes_query_failure_is_not_a_healthy_empty_release(make_checker):
    checker, calls, sleeps = make_checker(
        [ApiException(status=403, reason="Forbidden")]
    )
    result = checker.check_pods_with_polling()
    assert result["status"] is False
    assert "403" in result["details"][0]
    assert len(calls) == 1 and not sleeps
