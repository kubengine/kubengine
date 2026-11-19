"""既有 Kubernetes 资源的乐观更新、服务端预检及加密版本历史。"""

import copy
import hashlib
import json
import os
import stat
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import yaml
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from kubernetes.client.exceptions import ApiException
from sqlalchemy import func, select, text
from urllib3.exceptions import HTTPError

from core.config import Application
from core.http_api_client.kubernetes_view_client import (
    RESOURCES,
    resource_kind,
)
from core.orm.engine import get_db
from core.orm.resource_revision import ResourceRevision

MAX_YAML_BYTES = 2 * 1024**2
SERVER_META = {
    "uid",
    "resourceVersion",
    "generation",
    "creationTimestamp",
    "managedFields",
    "selfLink",
    "deletionTimestamp",
    "deletionGracePeriodSeconds",
}


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("YAML 字段名必须是字符串且不能重复")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)
# Kubernetes 对象以 JSON 表示；日期字段保持字符串。
UniqueLoader.yaml_implicit_resolvers = {
    key: [
        (tag, regex)
        for tag, regex in values
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for key, values in UniqueLoader.yaml_implicit_resolvers.items()
}


def load_manifests(source):
    """有界解析 Kubernetes YAML，允许多文档但不展开锚点。"""
    if len(source.encode()) > MAX_YAML_BYTES:
        raise HTTPException(413, "单个 YAML 最大 2 MiB")
    try:
        depth = count = 0
        for event in yaml.parse(source):
            count += 1
            if isinstance(event, yaml.AliasEvent):
                raise ValueError("不支持 YAML 锚点引用，请展开为普通字段")
            if isinstance(
                event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)
            ):
                depth += 1
            elif isinstance(
                event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)
            ):
                depth -= 1
            if depth > 64 or count > 100000:
                raise ValueError("YAML 结构过深或字段过多")
        values = list(yaml.load_all(source, Loader=UniqueLoader))
        json.dumps(values, allow_nan=False)
        return values
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        # YAML 解析错误包含原文片段，避免把 Secret 值回显到错误日志。
        raise HTTPException(
            400,
            "YAML 无效：请检查缩进、重复字段及字段类型（不支持锚点引用）",
        ) from None


def parse_manifest(source):
    values = load_manifests(source)
    if len(values) != 1 or not isinstance(values[0], dict):
        raise HTTPException(400, "请提供单个 Kubernetes 资源对象")
    return values[0]


def editable(value):
    value = copy.deepcopy(value)
    value.pop("status", None)
    for key in SERVER_META:
        value.get("metadata", {}).pop(key, None)
    return value


def yaml_text(value):
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def cluster_identity(view):
    configuration = view.api_client.configuration
    ca = getattr(configuration, "ssl_ca_cert", None)
    ca_hash = hashlib.sha256(Path(ca).read_bytes()).hexdigest() if ca else ""
    return digest([configuration.host.rstrip("/"), ca_hash])


def redact(value, secret, reveal):
    value = copy.deepcopy(value)
    if secret and not reveal:
        for key in ("data", "stringData"):
            if isinstance(value.get(key), dict):
                value[key] = {name: "<已隐藏>" for name in value[key]}
        annotations = value.get("metadata", {}).get("annotations")
        if annotations:
            value["metadata"]["annotations"] = {
                name: "<已隐藏>" for name in annotations
            }
    return value


def changes(before, after, secret=False, reveal=False):
    result = []

    def visit(old, new, path, old_exists=True, new_exists=True):
        if (
            old_exists
            and new_exists
            and type(old) is type(new)
            and not isinstance(old, (dict, list))
            and old == new
        ):
            return
        if (
            old_exists
            and new_exists
            and isinstance(old, dict)
            and isinstance(new, dict)
        ):
            for key in sorted(set(old) | set(new)):
                escaped = key.replace("~", "~0").replace("/", "~1")
                visit(
                    old.get(key),
                    new.get(key),
                    path + "/" + escaped,
                    key in old,
                    key in new,
                )
            return
        if (
            old_exists
            and new_exists
            and isinstance(old, list)
            and isinstance(new, list)
        ):
            for index in range(max(len(old), len(new))):
                visit(
                    old[index] if index < len(old) else None,
                    new[index] if index < len(new) else None,
                    path + "/" + str(index),
                    index < len(old),
                    index < len(new),
                )
            return
        hidden = (
            secret
            and not reveal
            and any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in ("/data", "/stringData", "/metadata/annotations")
            )
        )
        result.append(
            {
                "path": path or "/",
                "action": (
                    "add"
                    if not old_exists
                    else "remove" if not new_exists else "replace"
                ),
                "before": "<已隐藏>" if hidden and old_exists else old,
                "after": "<已隐藏>" if hidden and new_exists else new,
                "redacted": hidden,
            }
        )

    visit(before, after, "")
    if len(result) > 2000:
        raise HTTPException(413, "改动项超过 2000 项，请拆分修改")
    return result


def history_cipher():
    key_path = Path(Application.ROOT_DIR) / "config/resource-history.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        with get_db() as db:
            if (
                db.scalar(select(ResourceRevision.id).limit(1)) is not None
                and not key_path.exists()
            ):
                raise HTTPException(
                    503, "历史密钥缺失，请恢复 config/resource-history.key"
                )
        fd, temporary = tempfile.mkstemp(
            prefix=".resource-history-", dir=key_path.parent
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(temporary, key_path)
        finally:
            os.unlink(temporary)
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise HTTPException(
                503, "历史密钥须为服务账号所有的 0600 普通文件"
            )
        return Fernet(stream.read().strip())


def revision_summary(row):
    return {
        "id": row.id,
        "version": row.version,
        "actor": row.actor,
        "comment": row.comment,
        "status": row.status,
        "createdAt": row.created_at.isoformat() + "Z",
        "completedAt": (
            row.completed_at.isoformat() + "Z" if row.completed_at else None
        ),
        "beforeResourceVersion": row.before_rv,
        "afterResourceVersion": row.after_rv,
        "uid": row.resource_uid,
        "changedPaths": row.paths,
        "error": row.error,
    }


class ResourceYaml:
    def __init__(self, view, kind, namespace, name, authorizer=None):
        self.authorizer = authorizer
        try:
            self.kind = resource_kind(kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        self.view = view
        self.namespace = namespace if RESOURCES[self.kind][3] else ""
        self.name = name
        self.cluster = cluster_identity(view)

    def call(self, body=None, dry_run=False):
        if self.authorizer:
            self.authorizer(
                "resource.read" if body is None else "resource.update",
                self.kind,
                self.namespace,
                RESOURCES[self.kind][3],
                manifest=body,
            )
        api, stem, _, namespaced = RESOURCES[self.kind]
        kwargs = {
            "name": self.name,
            "_request_timeout": 30,
            "_preload_content": False,
        }
        if namespaced:
            kwargs["namespace"] = self.namespace
        method = "read_" if body is None else "replace_"
        if body is not None:
            kwargs.update(
                body=body,
                field_validation="Strict",
                field_manager="kubengine-yaml",
            )
            if dry_run:
                kwargs["dry_run"] = "All"
        sdk_api = getattr(self.view, api)
        method_name = method + ("namespaced_" if namespaced else "") + stem
        serializer = getattr(sdk_api, "_" + method_name + "_serialize", None)
        if serializer is not None:
            # SDK 37 的公开方法会先转成 Pydantic 模型，丢弃未知字段。
            # 直接使用其请求序列化器和已配置的 SDK 传输，保留原始 JSON。
            serialized_args = {
                key: value
                for key, value in kwargs.items()
                if not key.startswith("_")
            }
            serialized_args.update(
                pretty=None,
                _request_auth=None,
                _content_type="application/json",
                _headers={"Accept": "application/json"},
                _host_index=0,
            )
            if body is not None:
                serialized_args.setdefault("dry_run", None)
            response = self.view.api_client.call_api(
                *serializer(**serialized_args), _request_timeout=30
            )
        else:
            # SDK 34–36 的方法原样传递字典。
            response = getattr(sdk_api, method_name)(**kwargs)
        try:
            raw = response.data
            if raw is None:
                raw = response.read()
            value = json.loads(raw)
            if response.status >= 400:
                error = ApiException(
                    status=response.status, reason="Kubernetes 请求失败"
                )
                error.body = raw.decode() if isinstance(raw, bytes) else raw
                raise error
            return value
        finally:
            underlying = getattr(response, "response", response)
            with suppress(Exception):
                underlying.close()
            with suppress(Exception):
                underlying.release_conn()

    def scope(self, uid):
        return digest(
            [self.cluster, self.kind, self.namespace, self.name, uid]
        )

    def editor(self, reveal=False):
        current = self.call()
        meta = current["metadata"]
        return {
            "yaml": yaml_text(
                redact(editable(current), self.kind == "secret", reveal)
            ),
            "uid": meta["uid"],
            "resourceVersion": meta["resourceVersion"],
            "editable": self.kind != "event"
            and (self.kind != "secret" or reveal),
            "reason": (
                "事件由集群生成，仅供查看"
                if self.kind == "event"
                else (
                    "请先显式显示 Secret 值，再编辑 YAML"
                    if self.kind == "secret" and not reveal
                    else ""
                )
            ),
        }

    def prepare(self, data):
        if self.kind == "event":
            raise HTTPException(400, "事件由集群生成，仅供查看")
        if self.kind == "secret" and not data.reveal_secret:
            raise HTTPException(400, "编辑 Secret 需显式显示 Secret 值")
        desired = parse_manifest(data.yaml_text)
        current = self.call()
        meta = current["metadata"]
        if (
            data.uid != meta["uid"]
            or data.resource_version != meta["resourceVersion"]
        ):
            raise HTTPException(
                409, "资源已变更或已被重建，请重新载入 YAML 后合并修改"
            )
        if current.get("metadata", {}).get("deletionTimestamp"):
            raise HTTPException(409, "资源正在删除，不能修改")
        target_meta = desired.get("metadata")
        if not isinstance(target_meta, dict):
            raise HTTPException(400, "缺少 metadata")
        if (
            desired.get("apiVersion") != current.get("apiVersion")
            or desired.get("kind") != current.get("kind")
            or target_meta.get("name") != self.name
            or target_meta.get("namespace", "") != self.namespace
        ):
            raise HTTPException(
                400, "不能修改 apiVersion、kind、资源名称或命名空间"
            )
        if "status" in desired or SERVER_META.intersection(target_meta):
            raise HTTPException(
                400, "不能提交 status 或 metadata 中的服务端管理字段"
            )
        before = editable(current)
        body = copy.deepcopy(desired)
        body["metadata"].update(
            uid=data.uid, resourceVersion=data.resource_version
        )
        # 保留未知字段的原始 JSON，避免 SDK 模型反序列化时丢失新版本字段。
        preview = editable(self.call(body, dry_run=True))
        diff = changes(
            before, preview, self.kind == "secret", data.reveal_secret
        )
        return before, desired, body, preview, diff

    def preview(self, data):
        before, _, _, preview, diff = self.prepare(data)
        return {
            "changes": diff,
            "previewHash": digest(preview),
            "beforeYaml": yaml_text(before),
            "afterYaml": yaml_text(preview),
            "noChanges": not diff,
        }

    def apply(self, data, actor):
        # 重试在读取当前 resourceVersion 前处理，避免成功后重复写集群。
        scope = self.scope(data.uid)
        payload_hash = digest(
            [
                data.yaml_text,
                data.uid,
                data.resource_version,
                data.preview_hash,
                data.comment,
                actor,
            ]
        )
        with get_db() as db:
            prior = db.scalar(
                select(ResourceRevision).where(
                    ResourceRevision.scope == scope,
                    ResourceRevision.request_id == data.request_id,
                )
            )
            if prior is not None:
                if prior.payload_hash != payload_hash:
                    raise HTTPException(409, "该请求标识已用于其他修改")
                # 同时确认当前调用者仍有集群读取权限。
                self.call()
                return {"revision": revision_summary(prior), "replayed": True}
        before, desired, body, preview, diff = self.prepare(data)
        if digest(preview) != data.preview_hash:
            raise HTTPException(
                409, "校验结果已变化，请重新校验并预览后再应用"
            )
        if not diff:
            return {"noChanges": True}
        cipher = history_cipher()
        snapshots = {"before": before, "requested": desired, "after": preview}
        encrypted = cipher.encrypt(
            json.dumps(snapshots, ensure_ascii=False).encode()
        ).decode()
        # 版本号与幂等键跨进程串行分配，网络调用在提交事务之后进行。
        with get_db() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            prior = db.scalar(
                select(ResourceRevision).where(
                    ResourceRevision.scope == scope,
                    ResourceRevision.request_id == data.request_id,
                )
            )
            if prior is not None:
                if prior.payload_hash != payload_hash:
                    raise HTTPException(409, "该请求标识已用于其他修改")
                return {"revision": revision_summary(prior), "replayed": True}
            version = (
                db.scalar(
                    select(func.max(ResourceRevision.version)).where(
                        ResourceRevision.scope == scope
                    )
                )
                or 0
            ) + 1
            row = ResourceRevision(
                scope=scope,
                version=version,
                request_id=data.request_id,
                payload_hash=payload_hash,
                actor=actor,
                comment=data.comment,
                resource_uid=data.uid,
                before_rv=data.resource_version,
                paths=[item["path"] for item in diff],
                snapshots=encrypted,
            )
            db.add(row)
            db.commit()
            record_id = row.id
        try:
            result = self.call(body)
        except (ApiException, HTTPError, OSError) as exc:
            # 请求发出后连接超时不等于写入失败，保留可查询记录且不自动重放。
            known_failure = (
                isinstance(exc, ApiException)
                and 400 <= (exc.status or 0) < 500
            )
            with get_db() as db:
                row = db.get(ResourceRevision, record_id)
                row.status = "failed" if known_failure else "unknown"
                row.error = (
                    "集群拒绝修改"
                    if known_failure
                    else "连接中断，结果待核实，请刷新资源"
                )
                row.completed_at = datetime.utcnow()
                db.commit()
            raise
        snapshots["after"] = editable(result)
        try:
            encrypted = cipher.encrypt(
                json.dumps(snapshots, ensure_ascii=False).encode()
            ).decode()
            with get_db() as db:
                row = db.get(ResourceRevision, record_id)
                row.status = "applied"
                row.after_rv = result["metadata"]["resourceVersion"]
                row.completed_at = datetime.utcnow()
                row.snapshots = encrypted
                row.paths = [
                    item["path"]
                    for item in changes(before, snapshots["after"])
                ]
                db.commit()
                summary = revision_summary(row)
        except Exception:
            # pending 记录已在写集群前持久化；不能把已生效的变更报告为可安全重试的失败。
            return {
                "revision": {
                    "id": record_id,
                    "version": version,
                    "status": "pending",
                },
                "warning": "集群已接受修改，但版本结果未完成记录，请刷新核对，勿重复应用",
            }
        return {
            "revision": summary,
            "resourceVersion": summary["afterResourceVersion"],
        }

    def versions(self, page=1, page_size=20):
        current = self.call()  # 历史查询沿用当前资源的 Kubernetes 读取权限。
        scope = self.scope(current["metadata"]["uid"])
        with get_db() as db:
            query = select(ResourceRevision).where(
                ResourceRevision.scope == scope
            )
            total = db.scalar(
                select(func.count()).select_from(query.subquery())
            )
            rows = db.scalars(
                query.order_by(ResourceRevision.version.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
            return {
                "items": [revision_summary(row) for row in rows],
                "total": total,
                "uid": current["metadata"]["uid"],
            }

    def version(self, record_id, reveal=False):
        current = self.call()
        with get_db() as db:
            row = db.get(ResourceRevision, record_id)
            if row is None or row.scope != self.scope(
                current["metadata"]["uid"]
            ):
                raise HTTPException(404, "版本不存在或不属于当前资源")
            result = revision_summary(row)
            encrypted = row.snapshots
        try:
            snapshots = json.loads(
                history_cipher().decrypt(encrypted.encode())
            )
        except InvalidToken:
            raise HTTPException(
                503, "无法解密版本，请检查历史密钥是否正确"
            ) from None
        secret = self.kind == "secret"
        result.update(
            beforeYaml=yaml_text(redact(snapshots["before"], secret, reveal)),
            afterYaml=yaml_text(redact(snapshots["after"], secret, reveal)),
            requestedYaml=yaml_text(
                redact(snapshots["requested"], secret, reveal)
            ),
            changes=changes(
                (
                    {"metadata": {}}
                    if snapshots.get("operation") == "create"
                    else snapshots["before"]
                ),
                snapshots["after"],
                secret,
                reveal,
            ),
            redacted=secret and not reveal,
        )
        return result
