"""通过 SDK 发现资源类型，预检并逐项创建 YAML 资源，保留加密版本记录。"""

import copy
import hashlib
import hmac
import json
import re
import time
from contextlib import suppress
from datetime import datetime
from urllib.parse import quote

from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException
from sqlalchemy import func, select, text
from urllib3.exceptions import HTTPError

from core.auth_credentials import load_signing_secret
from core.orm.engine import get_db
from core.orm.resource_revision import ResourceRevision
from core.services.resource_yaml import (
    SERVER_META,
    changes,
    cluster_identity,
    digest,
    editable,
    history_cipher,
    load_manifests,
    redact,
    yaml_text,
)

MAX_RESOURCES = 20
PREVIEW_SECONDS = 600
LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
KIND = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,127}$")


def valid_label(value):
    return (
        isinstance(value, str) and len(value) <= 63 and LABEL.fullmatch(value)
    )


def api_prefix(version):
    if not isinstance(version, str):
        raise HTTPException(400, "缺少有效的 apiVersion")
    parts = version.split("/")
    if len(parts) == 1 and valid_label(parts[0]):
        return "/api/" + parts[0]
    if (
        len(parts) == 2
        and len(parts[0]) <= 253
        and all(valid_label(label) for label in parts[0].split("."))
        and valid_label(parts[1])
    ):
        return "/apis/" + version
    raise HTTPException(400, "apiVersion 格式不正确")


def parse_resources(source):
    values = []

    def append(value):
        if value is None:
            return
        if not isinstance(value, dict):
            raise HTTPException(
                400, "每个 YAML 文档必须是 Kubernetes 资源对象"
            )
        if value.get("kind") == "List":
            items = value.get("items")
            if not isinstance(items, list):
                raise HTTPException(400, "List.items 必须为资源数组")
            for item in items:
                append(item)
        else:
            values.append(value)
        if len(values) > MAX_RESOURCES:
            raise HTTPException(413, f"每次最多创建 {MAX_RESOURCES} 个资源")

    for document in load_manifests(source):
        append(document)
    if not values:
        raise HTTPException(400, "YAML 中没有可创建的资源")
    for index, value in enumerate(values, 1):
        api_prefix(value.get("apiVersion"))
        kind, meta = value.get("kind"), value.get("metadata")
        if not isinstance(kind, str) or not KIND.fullmatch(kind):
            raise HTTPException(400, f"第 {index} 个资源缺少有效的 kind")
        if not isinstance(meta, dict):
            raise HTTPException(400, f"第 {index} 个资源缺少 metadata")
        name = meta.get("name")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 253
            or name in {".", ".."}
            or any(char in name for char in ("/", "%", "\x00"))
        ):
            raise HTTPException(
                400, f"第 {index} 个资源必须提供有效的 metadata.name"
            )
        if "generateName" in meta:
            raise HTTPException(
                400, "创建资源须使用明确的 name，不支持 generateName"
            )
        if "status" in value or SERVER_META.intersection(meta):
            raise HTTPException(
                400, f"第 {index} 个资源请移除 status 和服务端管理字段"
            )
    return values


class SdkJson:
    """沿用 SDK 的认证、TLS、连接池，兼容 34–36 与 37 的原始 JSON 传输。"""

    def __init__(self, api_client, seconds):
        self.client = api_client
        self.deadline = time.monotonic() + seconds

    def call(self, method, path, body=None, dry_run=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("batch deadline")
        query = []
        if method == "POST":
            query = [
                ("fieldValidation", "Strict"),
                ("fieldManager", "kubengine-yaml-create"),
            ]
            if dry_run:
                query.append(("dryRun", "All"))
        params = dict(
            method=method,
            resource_path=path,
            path_params={},
            query_params=query,
            header_params={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=body,
            post_params=[],
            files={},
            auth_settings=["BearerToken"],
            collection_formats={},
        )
        timeout = (min(5, remaining), min(15, remaining))
        if hasattr(self.client, "param_serialize"):
            response = self.client.call_api(
                *self.client.param_serialize(**params),
                _request_timeout=timeout,
            )
        else:
            response = self.client.call_api(
                **params,
                response_type=None,
                _preload_content=False,
                _return_http_data_only=True,
                _request_timeout=timeout,
            )
        try:
            raw = response.data
            if raw is None:
                raw = response.read()
            if response.status >= 400:
                error = ApiException(
                    status=response.status, reason="Kubernetes 请求失败"
                )
                error.body = raw.decode() if isinstance(raw, bytes) else raw
                raise error
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                raise ApiException(
                    status=502, reason="集群返回了无效 JSON"
                ) from None
            if not isinstance(value, dict):
                raise ApiException(status=502, reason="集群响应不是资源对象")
            return value
        finally:
            underlying = getattr(response, "response", response)
            with suppress(Exception):
                underlying.close()
            with suppress(Exception):
                underlying.release_conn()


def failure(exc):
    """集群校验消息可能包含提交值，只返回状态及字段位置。"""
    code = getattr(exc, "status", None) or 503
    messages = {
        400: "资源结构或字段不正确，请检查 YAML",
        401: "集群认证失败，请检查 Kubernetes 连接凭据",
        403: "集群拒绝访问，请检查该资源的 RBAC 创建权限",
        404: "API 或命名空间不存在；请先创建命名空间或安装对应 CRD",
        409: "同名资源已存在，未覆盖；请进入资源详情修改 YAML",
        422: "资源字段未通过 Kubernetes 校验，请检查配置",
        429: "集群请求过于频繁，请稍后再试",
    }
    message = messages.get(code, "集群请求失败，请检查连接并核对资源状态")
    fields = []
    if isinstance(exc, ApiException):
        with suppress(ValueError, TypeError, AttributeError):
            fields = [
                item["field"]
                for item in (
                    json.loads(exc.body or "{}").get("details") or {}
                ).get("causes", [])
                if isinstance(item, dict)
                and isinstance(item.get("field"), str)
            ][:10]
    if fields:
        message += "；字段：" + "、".join(item[:150] for item in fields)
    return {"code": 502 if code == 401 else code, "message": message}


class ResourceCreator:
    def __init__(self, view, actor, seconds=60, authorizer=None):
        self.authorizer = authorizer
        self.view = view
        self.actor = actor
        self.cluster = cluster_identity(view)
        self.sdk = SdkJson(view.api_client, seconds)
        self.discovery = {}

    def targets(self, data):
        if not valid_label(data.namespace):
            raise HTTPException(400, "默认命名空间格式不正确")
        targets, seen = [], set()
        for index, value in enumerate(parse_resources(data.yaml_text), 1):
            prefix = api_prefix(value["apiVersion"])
            if prefix not in self.discovery:
                self.discovery[prefix] = self.sdk.call("GET", prefix)
            resource = next(
                (
                    r
                    for r in self.discovery[prefix].get("resources", [])
                    if r.get("kind") == value["kind"]
                    and "/" not in r.get("name", "")
                ),
                None,
            )
            if not resource or "create" not in resource.get("verbs", []):
                raise HTTPException(
                    400, f"第 {index} 个资源类型不存在或不支持创建"
                )
            plural = resource["name"]
            if not isinstance(plural, str) or not NAME.fullmatch(plural):
                raise HTTPException(502, "集群返回了无效的资源发现结果")
            body = copy.deepcopy(value)
            meta = body["metadata"]
            namespace = (
                meta.get("namespace", data.namespace)
                if resource["namespaced"]
                else ""
            )
            if resource["namespaced"] and not valid_label(namespace):
                raise HTTPException(
                    400, f"第 {index} 个资源的命名空间格式不正确"
                )
            if not resource["namespaced"] and meta.get("namespace"):
                raise HTTPException(
                    400, f"第 {index} 个资源属于集群级，不能指定 namespace"
                )
            if namespace:
                meta["namespace"] = namespace
            else:
                meta.pop("namespace", None)
            # 同一对象即使出现在两个 API 版本中，也不能重复提交。
            group = (
                body["apiVersion"].split("/")[0]
                if "/" in body["apiVersion"]
                else ""
            )
            identity = (group, plural, namespace, meta["name"])
            if identity in seen:
                raise HTTPException(
                    400, f"第 {index} 个资源与前面的资源名称重复"
                )
            seen.add(identity)
            path = (
                prefix
                + (
                    "/namespaces/" + quote(namespace, safe="")
                    if namespace
                    else ""
                )
                + "/"
                + plural
            )
            targets.append(
                {
                    "index": index,
                    "apiVersion": body["apiVersion"],
                    "kind": body["kind"],
                    "name": meta["name"],
                    "namespace": namespace,
                    "body": body,
                    "path": path,
                }
            )
        # 完整解析后逐项授权；此处尚未发生任何 dry-run 或真实写入。
        if self.authorizer:
            for target in targets:
                self.authorizer(
                    "resource.create",
                    target["kind"],
                    target["namespace"],
                    bool(target["namespace"]),
                    api_version=target["apiVersion"],
                    manifest=target["body"],
                )
        return targets

    def signature(self, data, expires):
        value = digest(
            [
                self.cluster,
                self.actor,
                data.yaml_text,
                data.namespace,
                data.comment,
                expires,
            ]
        )
        return hmac.new(
            load_signing_secret().encode(), value.encode(), hashlib.sha256
        ).hexdigest()

    def preview(self, data):
        targets = self.targets(data)
        namespaces = {
            t["name"]
            for t in targets
            if t["apiVersion"] == "v1" and t["kind"] == "Namespace"
        }
        items = []
        for target in targets:
            result = self.summary(target)
            try:
                value = self.sdk.call(
                    "POST", target["path"], target["body"], dry_run=True
                )
                result.update(
                    status="validated",
                    previewYaml=yaml_text(
                        redact(
                            editable(value), target["kind"] == "Secret", False
                        )
                    ),
                )
            except (ApiException, HTTPError, OSError) as exc:
                if (
                    isinstance(exc, ApiException)
                    and exc.status == 404
                    and target["namespace"] in namespaces
                ):
                    result.update(
                        status="deferred",
                        message="依赖本批新命名空间，提交时将先创建命名空间，再校验此资源",
                    )
                else:
                    result.update(status="failed", **failure(exc))
            items.append(result)
        valid = all(
            item["status"] in {"validated", "deferred"} for item in items
        )
        expires = int(time.time()) + PREVIEW_SECONDS
        return {
            "items": items,
            "valid": valid,
            "previewToken": (
                f"{expires}.{self.signature(data, expires)}" if valid else ""
            ),
        }

    @staticmethod
    def summary(target):
        return {
            k: target[k]
            for k in ("index", "apiVersion", "kind", "name", "namespace")
        }

    def verify(self, data):
        try:
            expires, signature = data.preview_token.split(".")
            expires = int(expires)
        except (ValueError, AttributeError):
            raise HTTPException(400, "请先完成集群预检") from None
        now = int(time.time())
        if (
            not now <= expires <= now + PREVIEW_SECONDS
            or not hmac.compare_digest(
                signature, self.signature(data, expires)
            )
        ):
            raise HTTPException(409, "预检已过期或 YAML 已变更，请重新预检")

    def request_key(self, data, target):
        return (
            "c-"
            + digest(
                [self.cluster, self.actor, data.request_id, target["index"]]
            )[:62]
        )

    def prior(self, db, key, payload_hash):
        row = db.scalar(
            select(ResourceRevision).where(ResourceRevision.request_id == key)
        )
        if row is not None and row.payload_hash != payload_hash:
            raise HTTPException(
                409, "该创建请求标识已用于其他内容，请重新预检"
            )
        return row

    def create(self, data):
        self.verify(data)
        targets = self.targets(data)
        cipher = history_cipher()  # 先确认可记录密文，再写入集群。
        hashes = {
            t["index"]: digest(
                [data.yaml_text, data.namespace, data.comment, t["body"]]
            )
            for t in targets
        }
        with get_db() as db:
            for target in targets:
                self.prior(
                    db, self.request_key(data, target), hashes[target["index"]]
                )
        results = []
        # Namespace 创建完成后，同批依赖资源才可进行真正的服务端预检。
        ordered = sorted(
            targets,
            key=lambda t: t["kind"] != "Namespace" or t["apiVersion"] != "v1",
        )
        for target in ordered:
            from core.authorization import (
                actor_context,
                operation_context,
                value,
            )
            from core.orm import audit

            event = None
            user = actor_context.get()
            if user is not None:
                operation = operation_context.get() or {}
                event = audit.begin_event(
                    actor_id=value(user, "user_id", ""),
                    username=value(user, "username", ""),
                    action="resource.create",
                    namespace=target["namespace"],
                    resource_type=target["kind"],
                    resource_name=target["name"],
                    parent_id=operation.get("audit_id", ""),
                    request_id=operation.get("request_id", ""),
                    batch_id=data.request_id,
                )
            try:
                result = self.create_one(
                    data, target, hashes[target["index"]], cipher
                )
            except Exception:
                # 已完成的资源不能因后续历史存储异常从批次结果中消失。
                result = {
                    **self.summary(target),
                    "status": "unknown",
                    "message": "创建记录未完成，请先查看资源状态，勿重复提交",
                }
            if event:
                audit.finish_event(
                    event,
                    (
                        200
                        if result["status"] in {"created", "replayed"}
                        else 500
                    ),
                    revision_id=(result.get("revision") or {}).get("id", ""),
                )
            results.append(result)
        results.sort(key=lambda item: item["index"])
        created = sum(item["status"] == "created" for item in results)
        return {
            "items": results,
            "created": created,
            "total": len(results),
            "complete": created == len(results),
        }

    def create_one(self, data, target, payload_hash, cipher):
        result = self.summary(target)
        key = self.request_key(data, target)
        kind = target["kind"].lower()
        pending_scope = digest(
            [self.cluster, kind, target["namespace"], target["name"], "create"]
        )
        snapshots = {
            "operation": "create",
            "before": {},
            "requested": target["body"],
            "after": target["body"],
        }
        encrypted = cipher.encrypt(
            json.dumps(snapshots, ensure_ascii=False).encode()
        ).decode()
        with get_db() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            prior = self.prior(db, key, payload_hash)
            if prior is not None:
                return self.record_result(result, prior, replayed=True)
            version = (
                db.scalar(
                    select(func.max(ResourceRevision.version)).where(
                        ResourceRevision.scope == pending_scope
                    )
                )
                or 0
            ) + 1
            row = ResourceRevision(
                scope=pending_scope,
                version=version,
                request_id=key,
                payload_hash=payload_hash,
                actor=self.actor,
                comment=data.comment or "通过 YAML 创建",
                resource_uid="",
                before_rv="",
                paths=[
                    c["path"]
                    for c in changes({"metadata": {}}, target["body"])
                ],
                snapshots=encrypted,
            )
            db.add(row)
            db.commit()
            record_id = row.id
        submitted = False
        try:
            self.sdk.call("POST", target["path"], target["body"], dry_run=True)
            submitted = True
            value = self.sdk.call("POST", target["path"], target["body"])
        except (ApiException, HTTPError, OSError) as exc:
            known = (
                not submitted
                or isinstance(exc, ApiException)
                and 400 <= (exc.status or 0) < 500
            )
            safe_error = failure(exc)
            with get_db() as db:
                row = db.get(ResourceRevision, record_id)
                row.status = "failed" if known else "unknown"
                row.error = (
                    safe_error["message"]
                    if known
                    else "连接中断，创建结果待核实；请查看资源，勿重复创建"
                )
                row.completed_at = datetime.utcnow()
                db.commit()
                return self.record_result(result, row, code=safe_error["code"])
        try:
            snapshots["after"] = editable(value)
            meta = value["metadata"]
            scope = digest(
                [
                    self.cluster,
                    kind,
                    target["namespace"],
                    target["name"],
                    meta["uid"],
                ]
            )
            encrypted = cipher.encrypt(
                json.dumps(snapshots, ensure_ascii=False).encode()
            ).decode()
            with get_db() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                row = db.get(ResourceRevision, record_id)
                version = (
                    db.scalar(
                        select(func.max(ResourceRevision.version)).where(
                            ResourceRevision.scope == scope,
                            ResourceRevision.id != record_id,
                        )
                    )
                    or 0
                ) + 1
                row.scope = scope
                row.version = version
                row.resource_uid = meta["uid"]
                row.after_rv = meta["resourceVersion"]
                row.snapshots = encrypted
                row.status = "applied"
                row.completed_at = datetime.utcnow()
                db.commit()
                return self.record_result(result, row)
        except Exception:
            # 集群已创建但历史结果未完成，保留 pending 记录防止自动重放。
            return {
                **result,
                "status": "unknown",
                "message": "集群已接受创建，但版本结果待记录；请查看资源，勿重复提交",
                "revisionId": record_id,
            }

    @staticmethod
    def record_result(result, row, replayed=False, code=None):
        status = (
            "created"
            if row.status == "applied"
            else "failed" if row.status == "failed" else "unknown"
        )
        return {
            **result,
            "status": status,
            "message": row.error
            or (
                "创建成功"
                if status == "created"
                else "创建结果待核实，请查看资源，勿重复提交"
            ),
            "revisionId": row.id,
            "version": row.version,
            "uid": row.resource_uid,
            "replayed": replayed,
            "code": code,
        }
