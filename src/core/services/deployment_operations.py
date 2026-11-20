"""Deployment 运维：先记录意图，至多一次写入，恢复只读核实。"""

import copy
import json
import time
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from urllib3.exceptions import HTTPError

from core import authorization as policy
from core.config import ConfigDict
from core.orm.audit import AuditEvent
from core.orm.deployment_operation import ACTIVE, DeploymentOperation
from core.orm.engine import get_db
from core.orm.identity import ConsoleUser, _public_user
from core.orm.resource_revision import ResourceRevision
from core.services.deployment_releases import (
    RELEASE_ACTIONS,
    DeploymentReleases,
    deployment_owned,
    patch_template,
)
from core.services.deployment_sdk import sdk_json
from core.services.resource_yaml import (
    ResourceYaml,
    digest,
    editable,
    history_cipher,
)
from core.task_runtime import lease_heartbeat

RESTART_MARKER = "kubengine.io/restartedAt"
DIAGNOSTICS = {
    "ErrImagePull": "镜像拉取失败",
    "ImagePullBackOff": "镜像拉取退避",
    "InvalidImageName": "镜像名称无效",
    "FailedScheduling": "Pod 调度失败",
    "FailedCreate": "副本创建失败，请检查配额和准入策略",
    "FailedMount": "卷挂载失败",
    "Unhealthy": "容器探针失败",
    "CrashLoopBackOff": "容器反复崩溃",
    "CreateContainerConfigError": "容器配置错误",
    "ExceededQuota": "配额不足",
}


def summary(row):
    return {
        "id": row.id,
        "requestId": row.request_id,
        "action": row.action,
        "namespace": row.namespace,
        "name": row.name,
        "uid": row.uid,
        "actor": row.actor,
        "status": row.status,
        "phase": row.phase,
        "writeStatus": row.write_status,
        "beforeReplicas": row.before_replicas,
        "targetReplicas": row.replicas,
        "targetGeneration": row.target_generation,
        "progress": row.progress,
        "reason": row.reason,
        "revisionId": row.revision_id,
        "auditId": row.audit_id,
        "release": row.release_context or {},
        "createdAt": row.created_at.isoformat() + "Z",
        "updatedAt": row.updated_at.isoformat() + "Z",
        "deadline": row.deadline.isoformat() + "Z",
        "completedAt": (
            row.completed_at.isoformat() + "Z" if row.completed_at else None
        ),
    }


def desired_manifest(current, action, replicas, marker):
    desired = copy.deepcopy(current)
    if action == "scale":
        desired["spec"]["replicas"] = replicas
    else:
        metadata = desired["spec"]["template"].setdefault("metadata", {})
        annotations = metadata.get("annotations") or {}
        metadata["annotations"] = dict(annotations, **{RESTART_MARKER: marker})
    return desired


def validate_target(current, uid, rv):
    meta, spec = current["metadata"], current["spec"]
    if (
        current.get("apiVersion") != "apps/v1"
        or current.get("kind") != "Deployment"
    ):
        raise HTTPException(409, "仅支持 apps/v1 Deployment")
    if meta["uid"] != uid or meta["resourceVersion"] != rv:
        raise HTTPException(409, "资源已变更或同名重建，请刷新后重新确认")
    if meta.get("deletionTimestamp"):
        raise HTTPException(409, "Deployment 正在删除")
    if spec.get("paused"):
        raise HTTPException(409, "Deployment 已暂停，请先通过既有流程处理")


class DeploymentOperations:
    def __init__(self, view, namespace, name, user):
        self.view, self.namespace, self.name, self.user = (
            view,
            namespace,
            name,
            user,
        )
        self.check = policy.authorizer(user)
        self.yaml = ResourceYaml(
            view, "deployment", namespace, name, self.check
        )
        self.target = digest([self.yaml.cluster, namespace, name])

    def read(self):
        return self.yaml.call()

    def list_objects(self, api, method, **kwargs):
        result, token = [], None
        while True:
            page = sdk_json(
                self.view,
                api,
                method,
                namespace=self.namespace,
                limit=500,
                _continue=token,
                **kwargs,
            )
            result.extend(page.get("items") or [])
            token = (page.get("metadata") or {}).get("continue")
            if not token:
                return result
            if len(result) >= 10000:
                raise HTTPException(
                    503, "命名空间对象过多，无法完整核实运维状态"
                )

    def hpas(self):
        self.check("resource.read", "horizontalpodautoscaler", self.namespace)
        items = self.list_objects(
            "autoscaling", "list_namespaced_horizontal_pod_autoscaler"
        )
        return [
            item["metadata"]["name"]
            for item in items
            if (item.get("spec") or {}).get("scaleTargetRef", {}).get("kind")
            == "Deployment"
            and item["spec"]["scaleTargetRef"].get("apiVersion") == "apps/v1"
            and item["spec"]["scaleTargetRef"].get("name") == self.name
        ]

    def get(self, operation_id):
        self.check("resource.read", "deployment", self.namespace)
        # 同时核对 Kubernetes 读取权限；资源删除后保留历史查询能力。
        try:
            self.read()
        except ApiException as exc:
            if exc.status != 404:
                raise
        with get_db() as db:
            row = db.get(DeploymentOperation, operation_id)
            if row is None or row.target != self.target:
                raise HTTPException(404, "操作不存在或不属于此 Deployment")
            return summary(row)

    def records(self):
        self.check("resource.read", "deployment", self.namespace)
        current, current_error = None, ""
        try:
            value = self.read()
            meta, spec = value["metadata"], value["spec"]
            try:
                hpas, hpa_error = self.hpas(), ""
            except (ApiException, HTTPError, OSError, HTTPException):
                hpas, hpa_error = [], "无法确认 HPA 状态，禁止手动扩缩容"
            current = {
                "uid": meta["uid"],
                "resourceVersion": meta["resourceVersion"],
                "generation": meta["generation"],
                "replicas": spec.get("replicas", 1),
                "strategy": (spec.get("strategy") or {}).get(
                    "type", "RollingUpdate"
                ),
                "rollingUpdate": (spec.get("strategy") or {}).get(
                    "rollingUpdate", {}
                ),
                "paused": bool(spec.get("paused")),
                "deleting": bool(meta.get("deletionTimestamp")),
                "progressDeadlineSeconds": spec.get("progressDeadlineSeconds"),
                "hpas": hpas,
                "hpaError": hpa_error,
                "nativeRevision": native_revision(value),
            }
        except ApiException as exc:
            if exc.status != 404:
                raise
            current_error = "Deployment 不存在，保留历史操作供核实"
        with get_db() as db:
            rows = db.scalars(
                select(DeploymentOperation)
                .where(DeploymentOperation.target == self.target)
                .order_by(DeploymentOperation.created_at.desc())
                .limit(50)
            ).all()
            return {
                "current": current,
                "currentError": current_error,
                "items": [summary(row) for row in rows],
            }

    def submit(self, action, body):
        if action not in {"scale", "restart", *RELEASE_ACTIONS}:
            raise HTTPException(422, "不支持的 Deployment 操作")
        self.check("resource.update", "deployment", self.namespace)
        context = {
            "actor_id": policy.value(self.user, "user_id"),
            "auth_revision": policy.value(self.user, "auth_revision"),
            "request_id": body.request_id,
            "parent_audit_id": (policy.operation_context.get() or {}).get(
                "audit_id", ""
            ),
        }
        payload = digest([action, body.model_dump(), context["actor_id"]])
        prior_id = None
        with get_db() as db:
            prior = db.scalar(
                select(DeploymentOperation).where(
                    DeploymentOperation.target == self.target,
                    DeploymentOperation.request_id == body.request_id,
                )
            )
            if prior is not None:
                if prior.payload_hash != payload:
                    raise HTTPException(
                        409, "该请求标识已用于其他参数或操作者"
                    )
                prior_id = prior.id
        if prior_id:
            prior_summary = self.get(prior_id)
            return {
                "operation": prior_summary,
                "replayed": True,
                "task_id": prior_id,
                "revision": {"id": prior_summary["revisionId"]},
            }
        current = self.read()
        validate_target(current, body.uid, body.resource_version)
        replicas = (
            body.replicas
            if action == "scale"
            else current["spec"].get("replicas", 1)
        )
        if action == "scale":
            if replicas == 0 and not body.confirm_zero:
                raise HTTPException(409, "缩容到 0 将停止服务，必须明确确认")
            if self.hpas():
                raise HTTPException(
                    409, "HPA 正在管理该 Deployment，禁止手动扩缩容"
                )
            if replicas == current["spec"].get("replicas", 1):
                raise HTTPException(
                    409, "目标副本数与当前配置相同，无需扩缩容"
                )
        operation_id, audit_id = uuid4().hex, uuid4().hex
        marker = (
            datetime.utcnow().isoformat(timespec="microseconds")
            + "Z-"
            + operation_id[:8]
        )
        release_context = {}
        if action in RELEASE_ACTIONS:
            requested, desired, release_context = DeploymentReleases(
                self
            ).verify(action, body, current)
        else:
            desired = desired_manifest(current, action, replicas, marker)
            requested = desired
        self.check(
            "resource.update",
            "deployment",
            self.namespace,
            api_version="apps/v1",
            manifest=desired,
        )
        before, after = editable(current), editable(desired)
        cipher = history_cipher()
        encrypted = cipher.encrypt(
            json.dumps(
                {
                    "before": before,
                    "requested": editable(requested),
                    "after": after,
                },
                ensure_ascii=False,
            ).encode()
        ).decode()
        seconds = int(
            ConfigDict.get_instance()
            .get("deployment_operations", {})
            .get("observation_seconds", 600)
        )
        seconds = max(30, min(86400, seconds))
        try:
            with get_db() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                prior = db.scalar(
                    select(DeploymentOperation).where(
                        DeploymentOperation.target == self.target,
                        DeploymentOperation.request_id == body.request_id,
                    )
                )
                if prior is not None:
                    if prior.payload_hash != payload:
                        raise HTTPException(
                            409, "该请求标识已用于其他参数或操作者"
                        )
                    return {
                        "operation": summary(prior),
                        "replayed": True,
                        "task_id": prior.id,
                        "revision": {"id": prior.revision_id},
                    }
                active = db.scalar(
                    select(DeploymentOperation.id).where(
                        DeploymentOperation.target == self.target,
                        DeploymentOperation.status.in_(ACTIVE),
                    )
                )
                if active:
                    raise HTTPException(
                        409,
                        "此 Deployment 已有进行中或待核实操作，请先查看记录",
                    )
                scope = self.yaml.scope(body.uid)
                version = (
                    db.scalar(
                        select(func.max(ResourceRevision.version)).where(
                            ResourceRevision.scope == scope
                        )
                    )
                    or 0
                ) + 1
                revision = ResourceRevision(
                    scope=scope,
                    version=version,
                    request_id="deploy-" + operation_id,
                    payload_hash=payload,
                    actor=policy.value(self.user, "username"),
                    comment="Deployment " + action + "，操作 " + operation_id,
                    resource_uid=body.uid,
                    before_rv=body.resource_version,
                    paths=release_context.get("changedPaths")
                    or [
                        (
                            "/spec/replicas"
                            if action == "scale"
                            else (
                                "/spec/template/metadata/annotations/"
                                "kubengine.io~1restartedAt"
                            )
                        )
                    ],
                    snapshots=encrypted,
                )
                db.add(revision)
                db.flush()
                event = AuditEvent(
                    event_id=audit_id,
                    created_at=int(time.time()),
                    actor_id=context["actor_id"],
                    username=policy.value(self.user, "username"),
                    action="deployment." + action,
                    namespace=self.namespace,
                    resource_type="deployment",
                    resource_name=self.name,
                    request_id=body.request_id,
                    result="pending",
                    task_id=operation_id,
                    revision_id=str(revision.id),
                    parent_id=context["parent_audit_id"],
                    batch_id=body.uid[:128],
                )
                db.add(event)
                row = DeploymentOperation(
                    id=operation_id,
                    target=self.target,
                    cluster=self.yaml.cluster,
                    namespace=self.namespace,
                    name=self.name,
                    uid=body.uid,
                    request_id=body.request_id,
                    payload_hash=payload,
                    action=action,
                    actor=policy.value(self.user, "username"),
                    auth_context=context,
                    before_rv=body.resource_version,
                    before_generation=current["metadata"]["generation"],
                    target_generation=current["metadata"]["generation"] + 1,
                    before_hash=digest(current["spec"]),
                    desired_hash=digest(desired["spec"]),
                    before_replicas=current["spec"].get("replicas", 1),
                    replicas=replicas,
                    marker=marker if action == "restart" else "",
                    revision_id=revision.id,
                    audit_id=audit_id,
                    release_context=release_context,
                    deadline=datetime.utcnow() + timedelta(seconds=seconds),
                )
                db.add(row)
                db.flush()
                result = summary(row)
                db.commit()
                return {
                    "operation": result,
                    "revision": {"id": revision.id},
                    "task_id": operation_id,
                }
        except SQLAlchemyError:
            raise HTTPException(
                503, "无法保存运维操作与审计，集群尚未修改"
            ) from None


def native_revision(value):
    revision = str(
        value.get("metadata", {})
        .get("annotations", {})
        .get("deployment.kubernetes.io/revision", "")
    )
    return revision if revision.isdigit() and len(revision) < 20 else ""


def load_actor(row, before_write):
    with get_db() as db:
        user = db.get(ConsoleUser, row.auth_context.get("actor_id"))
        if (
            user is None
            or not user.enabled
            or (
                before_write
                and user.auth_revision != row.auth_context.get("auth_revision")
            )
        ):
            raise HTTPException(403, "发起者已停用、凭据变更或权限已撤销")
        result = _public_user(db, user)
        result["auth_revision"] = user.auth_revision
        policy.authorize(
            result, "resource.update", "deployment", row.namespace
        )
        return result


def template_hash(template):
    template = copy.deepcopy(template)
    metadata = template.setdefault("metadata", {})
    labels = metadata.get("labels") or {}
    labels.pop("pod-template-hash", None)
    metadata["labels"] = labels
    return digest(template)


def owned(item, uid):
    return any(
        ref.get("uid") == uid and ref.get("controller") is True
        for ref in item.get("metadata", {}).get("ownerReferences", [])
    )


def observe(ops, row, value):
    """返回健康结果和不含任意上游文本的诊断摘要。"""
    meta, spec, status = (
        value["metadata"],
        value["spec"],
        value.get("status") or {},
    )
    if meta["uid"] != row.uid or meta.get("deletionTimestamp"):
        return "superseded", "资源已删除或同名重建", {}
    generation = meta["generation"]
    spec_hash = digest(spec)
    if generation > row.target_generation or (
        generation >= row.target_generation and spec_hash != row.desired_hash
    ):
        return "superseded", "后续配置变更已替代本操作", {}
    if generation < row.target_generation:
        if generation != row.before_generation or spec_hash != row.before_hash:
            return "superseded", "资源配置已由其他操作修改", {}
        return "running", "写入结果尚未核实，禁止自动重放", {}
    rs = [
        item
        for item in ops.list_objects("apps", "list_namespaced_replica_set")
        if (
            deployment_owned(item, value)
            if row.action in RELEASE_ACTIONS
            else owned(item, row.uid)
        )
    ]
    template = template_hash(spec["template"])
    old = sum(
        max(
            item.get("status", {}).get("replicas", 0),
            item.get("spec", {}).get("replicas", 0),
        )
        + (item.get("status", {}).get("terminatingReplicas") or 0)
        for item in rs
        if template_hash(item["spec"]["template"]) != template
    )
    rs_total = sum(
        (item.get("status", {}).get("replicas") or 0)
        + (item.get("status", {}).get("terminatingReplicas") or 0)
        for item in rs
    )
    rs_uids = {item["metadata"]["uid"] for item in rs}
    pods = [
        item
        for item in ops.list_objects("core", "list_namespaced_pod")
        if any(owned(item, uid) for uid in rs_uids)
    ]
    diagnostics = set()
    for pod in pods:
        for container in [
            *(pod.get("status", {}).get("containerStatuses") or []),
            *(pod.get("status", {}).get("initContainerStatuses") or []),
        ]:
            reason = (container.get("state", {}).get("waiting") or {}).get(
                "reason"
            )
            if reason in DIAGNOSTICS:
                diagnostics.add(reason)
        if any(
            condition.get("type") == "PodScheduled"
            and condition.get("status") == "False"
            for condition in pod.get("status", {}).get("conditions") or []
        ):
            diagnostics.add("FailedScheduling")
    uids = rs_uids | {row.uid} | {item["metadata"]["uid"] for item in pods}
    for event in ops.list_objects("core", "list_namespaced_event"):
        reason = event.get("reason")
        if (
            event.get("involvedObject", {}).get("uid") in uids
            and reason in DIAGNOSTICS
        ):
            diagnostics.add(reason)
    progress = {
        "generation": generation,
        "observedGeneration": status.get("observedGeneration", 0),
        "target": row.replicas,
        "updated": status.get("updatedReplicas", 0),
        "ready": status.get("readyReplicas", 0),
        "available": status.get("availableReplicas", 0),
        "total": status.get("replicas", 0),
        "old": old,
        "replicaSetTotal": rs_total,
        "terminating": status.get("terminatingReplicas", 0) or 0,
        "nativeRevision": native_revision(value),
        "diagnostics": [
            {"code": key, "message": DIAGNOSTICS[key]}
            for key in sorted(diagnostics)
        ],
        "pods": [{"name": pod["metadata"]["name"]} for pod in pods[:30]],
    }
    if status.get("observedGeneration", 0) < row.target_generation:
        return "running", "等待 Deployment 控制器观察本次配置", progress
    for condition in status.get("conditions") or []:
        if (
            condition.get("type") == "Progressing"
            and condition.get("reason") == "ProgressDeadlineExceeded"
            and condition.get("status") == "False"
        ):
            return (
                "failed",
                "ProgressDeadlineExceeded：发布超过集群进度期限",
                progress,
            )
        if (
            condition.get("type") == "ReplicaFailure"
            and condition.get("status") == "True"
        ):
            return (
                "failed",
                "ReplicaFailure：副本创建失败，请检查配额和准入策略",
                progress,
            )
    complete = all(
        progress[key] == row.replicas
        for key in (
            "updated",
            "ready",
            "available",
            "total",
            "replicaSetTotal",
        )
    )
    # 防止 status 已归零但旧 Pod 仍在终止时误报完成。
    if (
        complete
        and old == 0
        and progress["terminating"] == 0
        and len(pods) == row.replicas
        and not any(pod["metadata"].get("deletionTimestamp") for pod in pods)
    ):
        return "succeeded", "目标配置已被观察且所有副本均就绪可用", progress
    return "running", "正在等待目标副本及旧副本退出", progress


def claim_operation():
    now, token = datetime.utcnow(), uuid4().hex
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.scalar(
            select(DeploymentOperation)
            .where(
                DeploymentOperation.status.in_(ACTIVE),
                DeploymentOperation.next_poll <= now,
                (DeploymentOperation.lease_until.is_(None))
                | (DeploymentOperation.lease_until <= now),
            )
            .order_by(DeploymentOperation.next_poll)
            .limit(1)
        )
        if row is None:
            return None
        row.lease_owner, row.lease_until = token, now + timedelta(seconds=180)
        operation_id = row.id
        db.commit()
        return operation_id, token


def fenced(db, operation_id, token):
    row = db.get(DeploymentOperation, operation_id)
    if (
        row is None
        or row.lease_owner != token
        or row.lease_until <= datetime.utcnow()
    ):
        raise RuntimeError("Deployment 运维租约已失效")
    return row


def renew_operation(operation_id, token):
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = fenced(db, operation_id, token)
        row.lease_until = datetime.utcnow() + timedelta(seconds=180)
        db.commit()
        return True


def save_step(operation_id, token, **values):
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = fenced(db, operation_id, token)
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.utcnow()
        row.next_poll = row.updated_at + timedelta(seconds=5)
        row.lease_owner, row.lease_until = "", None
        if row.status not in ACTIVE:
            row.completed_at = row.updated_at
        db.commit()


def record_write(operation_id, token, status, value=None):
    """版本和执行审计在同一事务收尾，与健康观察状态分离。"""
    with get_db() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = fenced(db, operation_id, token)
        row.write_status = status
        revision = db.get(ResourceRevision, row.revision_id)
        revision.status = {
            "accepted": "applied",
            "rejected": "failed",
            "unknown": "unknown",
        }[status]
        revision.completed_at = datetime.utcnow()
        if value is not None:
            revision.after_rv = value["metadata"]["resourceVersion"]
            cipher = history_cipher()
            snapshots = json.loads(cipher.decrypt(revision.snapshots.encode()))
            snapshots["after"] = editable(value)
            revision.snapshots = cipher.encrypt(
                json.dumps(snapshots, ensure_ascii=False).encode()
            ).decode()
        event = db.get(AuditEvent, row.audit_id)
        if event.result == "pending":
            event.result = status
            event.status_code = (
                200
                if status == "accepted"
                else 409 if status == "rejected" else 503
            )
            event.completed_at = int(time.time())
        db.commit()


def process_operation(operation_id, token, view):
    with get_db() as db:
        row = fenced(db, operation_id, token)
        db.expunge(row)
    pending = row.phase == "queued"
    try:
        if pending and datetime.utcnow() >= row.deadline:
            record_write(operation_id, token, "rejected")
            save_step(
                operation_id,
                token,
                status="failed",
                phase="queued",
                reason="等待执行超过平台观察期限，未执行写入",
            )
            return
        user = load_actor(row, pending)
        ops = DeploymentOperations(view, row.namespace, row.name, user)
        if ops.yaml.cluster != row.cluster:
            save_step(
                operation_id,
                token,
                status="timed_out",
                reason="集群连接配置已变化，需恢复原连接后核实",
            )
            return
        value = ops.read()
        if pending:
            validate_target(value, row.uid, row.before_rv)
            if row.action in RELEASE_ACTIONS:
                releases = DeploymentReleases(ops)
                releases.verify_source(row, value)
                if row.action == "image":
                    releases.replicasets(value)
                with get_db() as db:
                    revision = db.get(ResourceRevision, row.revision_id)
                    snapshots = json.loads(
                        history_cipher().decrypt(revision.snapshots.encode())
                    )
                desired = copy.deepcopy(value)
                desired["spec"]["template"] = snapshots["requested"]["spec"][
                    "template"
                ]
                ops.check(
                    "resource.update",
                    "deployment",
                    row.namespace,
                    api_version="apps/v1",
                    manifest=desired,
                )
                preview = patch_template(
                    ops, value, desired["spec"]["template"], dry_run=True
                )
                if digest(preview["spec"]) != row.desired_hash:
                    raise HTTPException(409, "准入预检结果已变化，未执行发布")
                ops.check(
                    "resource.update",
                    "deployment",
                    row.namespace,
                    api_version="apps/v1",
                    manifest=preview,
                )
            else:
                desired = desired_manifest(
                    value, row.action, row.replicas, row.marker
                )
            ops.check(
                "resource.update",
                "deployment",
                row.namespace,
                api_version="apps/v1",
                manifest=preview if row.action in RELEASE_ACTIONS else desired,
            )
            if row.action == "scale" and ops.hpas():
                raise HTTPException(409, "HPA 已接管，禁止手动扩缩容")
            # 最后一次授权核实在外部写入前；事务标记使崩溃恢复不会重放。
            user = load_actor(row, True)
            policy.authorizer(user)(
                "resource.update",
                "deployment",
                row.namespace,
                manifest=preview if row.action in RELEASE_ACTIONS else desired,
            )
            with get_db() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                claimed = fenced(db, operation_id, token)
                claimed.status, claimed.phase = "running", "writing"
                db.commit()
            row.phase = "writing"
            try:
                if row.action == "scale":
                    sdk_json(
                        view,
                        "apps",
                        "replace_namespaced_deployment_scale",
                        name=row.name,
                        namespace=row.namespace,
                        body={
                            "apiVersion": "autoscaling/v1",
                            "kind": "Scale",
                            "metadata": {
                                "uid": row.uid,
                                "resourceVersion": row.before_rv,
                                "name": row.name,
                                "namespace": row.namespace,
                            },
                            "spec": {"replicas": row.replicas},
                        },
                    )
                elif row.action in RELEASE_ACTIONS:
                    patch_template(ops, value, desired["spec"]["template"])
                else:
                    sdk_json(
                        view,
                        "apps",
                        "patch_namespaced_deployment",
                        name=row.name,
                        namespace=row.namespace,
                        content_type="application/strategic-merge-patch+json",
                        body={
                            "metadata": {
                                "uid": row.uid,
                                "resourceVersion": row.before_rv,
                            },
                            "spec": {
                                "template": {
                                    "metadata": {
                                        "annotations": {
                                            RESTART_MARKER: row.marker
                                        }
                                    }
                                }
                            },
                        },
                    )
            except (ApiException, HTTPError, OSError) as exc:
                known = (
                    isinstance(exc, ApiException)
                    and 400 <= (exc.status or 0) < 500
                )
                record_write(
                    operation_id, token, "rejected" if known else "unknown"
                )
                save_step(
                    operation_id,
                    token,
                    status="failed" if known else "timed_out",
                    phase="verifying",
                    reason=(
                        "集群明确拒绝写入"
                        if known
                        else "写入响应丢失，继续只读核实，禁止重复提交"
                    ),
                )
                return
            # SDK 返回 Scale 时须重新读取完整 Deployment，不能以 Scale 响应判成功。
            record_write(operation_id, token, "accepted")
            save_step(
                operation_id,
                token,
                status="running",
                phase="observing",
                reason="集群已接受写入，等待健康观察",
            )
            return
        state, reason, progress = observe(ops, row, value)
        if (
            value["metadata"]["uid"] == row.uid
            and value["metadata"]["generation"] == row.target_generation
            and digest(value["spec"]) == row.desired_hash
        ):
            record_write(operation_id, token, "accepted", value)
        elif row.write_status == "pending":
            record_write(operation_id, token, "unknown")
        if state == "running" and datetime.utcnow() >= row.deadline:
            state, reason = (
                "timed_out",
                "超过平台观察期限，继续只读核实；不会重放写入或自动回滚",
            )
        save_step(
            operation_id,
            token,
            status=state,
            phase="verifying" if state == "timed_out" else "observing",
            reason=reason,
            progress=progress,
        )
    except (ApiException, HTTPError, OSError, HTTPException) as exc:
        code = getattr(exc, "status_code", getattr(exc, "status", 503))
        pending = row.phase == "queued"
        if pending:
            state = (
                "permission_revoked"
                if code in {401, 403}
                else (
                    "superseded"
                    if code == 404
                    else "failed" if code in {400, 409, 422} else "pending"
                )
            )
        else:
            state = (
                "superseded"
                if code == 404
                else (
                    "timed_out"
                    if datetime.utcnow() >= row.deadline or code in {401, 403}
                    else "running"
                )
            )
        if pending and state not in ACTIVE:
            record_write(operation_id, token, "rejected")
        reason = (
            "执行前授权已撤销，未执行写入"
            if state == "permission_revoked"
            else (
                "目标已删除或被后续配置修改"
                if state in {"superseded", "failed"}
                else "集群或授权暂不可用，等待只读核实"
            )
        )
        if (
            pending
            and isinstance(exc, HTTPException)
            and isinstance(exc.detail, str)
        ):
            reason = exc.detail[:500]
        save_step(
            operation_id,
            token,
            status=state,
            reason=reason,
            phase=row.phase,
        )


def poll_operations(view_factory, limit=4):
    for _ in range(limit):
        claim = claim_operation()
        if claim is None:
            return
        operation_id, token = claim
        try:
            with lease_heartbeat(
                lambda: renew_operation(operation_id, token), interval=30
            ):
                process_operation(operation_id, token, view_factory())
        except Exception:
            # 任意落盘失败/进程中断由持久化 writing 阶段保证不重放。
            # 不输出任意异常正文，避免 SDK 异常带出资源敏感内容。
            from core.logger import get_logger

            get_logger(__name__).error(
                "Deployment 操作 %s 中断，租约到期后只读核实", operation_id
            )
