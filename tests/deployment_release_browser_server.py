"""本机隔离浏览器验收服务；只使用临时数据库、模拟 SDK 和构建产物。"""

import argparse
import copy
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# 必须先加载隔离配置，禁止读取工作区真实数据库或连接真实 Kubernetes。
import conftest  # noqa: F401
from core import authorization as policy
from core.orm.audit import AuditEvent
from core.orm.deployment_operation import DeploymentOperation
from core.orm.engine import Base, engine, get_db
from core.orm.identity import ConsoleUser, NamespaceBinding
from core.orm.resource_revision import ResourceRevision
from core.services.deployment_operations import (
    claim_operation,
    process_operation,
)
from test_deployment_operations import user
from test_deployment_releases import ReleaseCluster
from web.api import deployment_operations as routes
from web.utils import auth


class BrowserCluster(ReleaseCluster):
    def update(self, body):
        if self.failure:
            raise self.failure
        assert body["metadata"]["uid"] == self.value["metadata"]["uid"]
        assert (
            body["metadata"]["resourceVersion"]
            == self.value["metadata"]["resourceVersion"]
        )
        with get_db() as db:
            assert (
                db.query(DeploymentOperation)
                .filter_by(phase="writing")
                .count()
                >= 1
            )
        self.writes.append(copy.deepcopy(body))
        self.value["metadata"]["resourceVersion"] = str(
            int(self.value["metadata"]["resourceVersion"]) + 1
        )
        self.value["metadata"]["generation"] += 1

    def healthy(self, replicas=None):
        previous = copy.deepcopy(self.rs)
        super().healthy(replicas)
        generation = self.value["metadata"]["generation"]
        uid = "rs-generation-" + str(generation)
        meta = self.rs[0]["metadata"]
        meta.update(
            uid=uid,
            name="example-" + str(generation),
            annotations={
                "deployment.kubernetes.io/revision": str(generation + 1)
            },
        )
        meta["ownerReferences"][0].update(
            apiVersion="apps/v1", kind="Deployment", name="example"
        )
        for pod in self.pods:
            pod["metadata"]["ownerReferences"][0]["uid"] = uid
        self.value["metadata"]["annotations"] = {
            "deployment.kubernetes.io/revision": str(generation + 1)
        }
        for rs in previous:
            if rs["metadata"]["uid"] != uid:
                rs["spec"]["replicas"] = rs["status"]["replicas"] = 0
                self.rs.append(rs)


cluster = BrowserCluster()
cluster.healthy()
guard = threading.Lock()
stop = threading.Event()


def reset():
    global cluster
    with guard:
        cluster = BrowserCluster()
        cluster.healthy()
        with get_db() as db:
            for model in (
                DeploymentOperation,
                ResourceRevision,
                AuditEvent,
                NamespaceBinding,
                ConsoleUser,
            ):
                db.query(model).delete()
            for role in ("platform_admin", "namespace_operator", "readonly"):
                db.add(
                    ConsoleUser(
                        user_id=role,
                        username=role,
                        password_hash="disabled",
                        role=role,
                        enabled=True,
                        auth_revision=1,
                        created_at=0,
                        updated_at=0,
                    )
                )
                db.add(NamespaceBinding(user_id=role, namespace="apps"))
            db.commit()


async def identity(authorization=None, **kwargs):
    role = (authorization or "").removeprefix("Bearer ").split(".")[0]
    if role not in {"platform_admin", "namespace_operator", "readonly"}:
        raise HTTPException(401, "隔离验收会话失效")
    with get_db() as db:
        row = db.get(ConsoleUser, role)
        if not row.enabled:
            raise HTTPException(401, "隔离验收会话失效")
        value = user(row.role)
        value.update(
            user_id=role, username=role, auth_revision=row.auth_revision
        )
        value["namespaces"] = [
            binding.namespace
            for binding in db.query(NamespaceBinding).filter_by(user_id=role)
        ]
    return SimpleNamespace(**value), "isolated"


def worker():
    while not stop.wait(0.4):
        with guard:
            with get_db() as db:
                for row in db.query(DeploymentOperation).filter(
                    DeploymentOperation.status.in_(
                        ["pending", "running", "timed_out"]
                    )
                ):
                    row.next_poll = datetime.utcnow() - timedelta(seconds=1)
                db.commit()
            claim = claim_operation()
            if claim:
                process_operation(*claim, cluster)
                if (
                    "broken"
                    not in cluster.value["spec"]["template"]["spec"][
                        "containers"
                    ][0]["image"]
                ):
                    cluster.healthy()
                else:
                    cluster.healthy()
                    cluster.value["status"].update(
                        updatedReplicas=0,
                        readyReplicas=0,
                        availableReplicas=0,
                        conditions=[
                            {
                                "type": "Progressing",
                                "reason": "ProgressDeadlineExceeded",
                                "status": "False",
                            }
                        ],
                    )
                    cluster.pods[0]["status"] = {
                        "containerStatuses": [
                            {
                                "state": {
                                    "waiting": {"reason": "ImagePullBackOff"}
                                }
                            }
                        ]
                    }


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    reset()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=3)


app = FastAPI(lifespan=lifespan)
auth.get_current_user = identity
routes.get_kubernetes_client = lambda: cluster
app.include_router(routes.router, prefix="/api/v1/k8s")


@app.get("/api/v1/me")
async def me(request: Request):
    actor, _ = await identity(request.headers.get("authorization"))
    data = vars(actor)
    data["revision"] = actor.auth_revision
    data["permissions"] = ["resource.read"] + (
        ["resource.update"] if actor.role != "readonly" else []
    )
    return {"code": 200, "message": "success", "data": data}


@app.get("/api/v1/k8s/manifest/{kind}/{namespace}/{name}")
async def manifest(request: Request, kind: str, namespace: str, name: str):
    actor, _ = await identity(request.headers.get("authorization"))
    policy.authorize(actor, "resource.read", kind, namespace)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "manifest": cluster.value,
            "yaml": yaml.safe_dump(cluster.value),
        },
    }


@app.get("/api/v1/health")
def health():
    return {"status": "healthy", "environment": "isolated-sdk"}


@app.post("/__test__/reset")
def reset_test():
    reset()
    return {"ok": True}


@app.post("/__test__/{mode}")
def control(mode: str):
    with guard:
        if mode == "lost-response":
            cluster.crash_after = True
        elif mode == "clear-history":
            cluster.rs = []
        elif mode == "revoke":
            with get_db() as db:
                db.get(ConsoleUser, "namespace_operator").role = "readonly"
                db.get(ConsoleUser, "namespace_operator").auth_revision += 1
                db.commit()
        elif mode == "unauthenticated":
            with get_db() as db:
                db.get(ConsoleUser, "namespace_operator").enabled = False
                db.commit()
        else:
            raise HTTPException(404)
    return {"ok": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--static-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    static_dir = args.static_dir.resolve()
    app.mount(
        "/assets", StaticFiles(directory=static_dir / "assets"), name="assets"
    )

    @app.get("/{path:path}")
    def frontend(path: str):
        target = (static_dir / path).resolve()
        if not target.is_relative_to(static_dir):
            raise HTTPException(404)
        return FileResponse(
            target if target.is_file() else static_dir / "index.html"
        )

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
