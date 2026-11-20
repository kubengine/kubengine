"""模板发布预检、原生历史与绑定身份的短期凭证。"""

import copy
import json
import re
from datetime import datetime, timedelta

from cryptography.fernet import InvalidToken
from fastapi import HTTPException

from core import authorization as policy
from core.services.deployment_sdk import sdk_json
from core.services.resource_yaml import (
    SERVER_META,
    changes,
    digest,
    history_cipher,
)

RELEASE_ACTIONS = {"image", "rollback"}
PREVIEW_SECONDS = 300
REVISION = "deployment.kubernetes.io/revision"
# 基本 distribution 引用语法；不连接仓库、不判断内容是否存在。
COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
IMAGE = re.compile(
    rf"^(?:(?:[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?"
    rf"(?::[0-9]+)?|\[[0-9a-fA-F:]+\](?::[0-9]+)?)/)?"
    rf"{COMPONENT}(?:/{COMPONENT})*"
    r"(?::[\w][\w.-]{0,127})?"
    r"(?:@sha256:[0-9a-fA-F]{64})?$",
    re.ASCII,
)


def image_reference(value):
    last = value.rsplit("/", 1)[-1]
    if (
        len(value) > 512
        or not IMAGE.fullmatch(value)
        or (":" not in last and "@" not in last)
    ):
        raise HTTPException(
            422, "镜像引用无效，请填写完整名称及 tag 或 sha256 digest"
        )
    return value


def normalized_template(template):
    result = copy.deepcopy(template)
    metadata = result.setdefault("metadata", {})
    for key in SERVER_META:
        metadata.pop(key, None)
    labels = metadata.get("labels") or {}
    labels.pop("pod-template-hash", None)
    metadata["labels"] = labels
    return result


def deployment_owned(item, current):
    refs = [
        ref
        for ref in item.get("metadata", {}).get("ownerReferences", [])
        if ref.get("controller") is True
    ]
    return len(refs) == 1 and all(
        refs[0].get(key) == expected
        for key, expected in {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "uid": current["metadata"]["uid"],
            "name": current["metadata"]["name"],
        }.items()
    )


def images(template):
    return [
        {"name": item["name"], "image": item.get("image", "")}
        for item in template.get("spec", {}).get("containers", [])
    ]


def template_patch(current, template):
    return [
        {
            "op": "test",
            "path": "/metadata/uid",
            "value": current["metadata"]["uid"],
        },
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": current["metadata"]["resourceVersion"],
        },
        {"op": "replace", "path": "/spec/template", "value": template},
    ]


def patch_template(ops, current, template, *, dry_run=False):
    return sdk_json(
        ops.view,
        "apps",
        "patch_namespaced_deployment",
        name=ops.name,
        namespace=ops.namespace,
        body=template_patch(current, template),
        content_type="application/json-patch+json",
        field_validation="Strict",
        field_manager="kubengine-deployment",
        **({"dry_run": "All"} if dry_run else {}),
    )


def safe_changes(before, after):
    result = changes(before, after)
    for item in result:
        if not re.fullmatch(
            r"/spec/template/spec/containers/\d+/image", item["path"]
        ):
            item.update(before="<已隐藏>", after="<已隐藏>", redacted=True)
    return result


class DeploymentReleases:
    def __init__(self, ops):
        self.ops = ops

    def replicasets(self, current):
        self.ops.check("resource.read", "replicaset", self.ops.namespace)
        return [
            item
            for item in self.ops.list_objects(
                "apps", "list_namespaced_replica_set"
            )
            if deployment_owned(item, current)
        ]

    def history_items(self, current):
        rs = self.replicasets(current)
        counts = {}
        for item in rs:
            revision = (
                item.get("metadata", {})
                .get("annotations", {})
                .get(REVISION, "")
            )
            counts[revision] = counts.get(revision, 0) + 1
        result = []
        for item in rs:
            meta = item["metadata"]
            revision = str(meta.get("annotations", {}).get(REVISION, ""))
            template = (item.get("spec") or {}).get("template")
            reason = ""
            if not re.fullmatch(r"[1-9][0-9]{0,18}", revision):
                reason = "原生 revision 缺失或无效"
            elif counts.get(revision, 0) != 1:
                reason = "存在重复 revision，历史归属有歧义"
            elif meta.get("deletionTimestamp"):
                reason = "ReplicaSet 正在删除"
            elif not template or not template.get("spec", {}).get(
                "containers"
            ):
                reason = "历史 Pod 模板缺失"
            elif digest(normalized_template(template)) == digest(
                normalized_template(current["spec"]["template"])
            ):
                reason = "与当前 Pod 模板相同，无需回滚"
            elif current["metadata"].get("deletionTimestamp"):
                reason = "Deployment 正在删除"
            elif current["spec"].get("paused"):
                reason = "Deployment 已暂停"
            result.append(
                {
                    "revision": (
                        revision
                        if re.fullmatch(r"[1-9][0-9]{0,18}", revision)
                        else ""
                    ),
                    "replicaSet": meta["name"],
                    "replicaSetUid": meta["uid"],
                    "createdAt": meta.get("creationTimestamp"),
                    "containers": images(template or {}),
                    "canRollback": not reason,
                    "reason": reason,
                }
            )
        result.sort(
            key=lambda item: (
                int(item["revision"])
                if re.fullmatch(r"[1-9][0-9]{0,18}", item["revision"])
                else -1
            ),
            reverse=True,
        )
        return result, rs

    def history(self):
        self.ops.check("resource.read", "deployment", self.ops.namespace)
        current = self.ops.read()
        items, _ = self.history_items(current)
        return {
            "uid": current["metadata"]["uid"],
            "resourceVersion": current["metadata"]["resourceVersion"],
            "containers": images(current["spec"]["template"]),
            "items": items,
            "limitation": "仅展示仍保留的原生 ReplicaSet；已清理的 revision 无法回滚。",
        }

    def source(self, current, revision, rs_uid):
        items, rs = self.history_items(current)
        match = next(
            (
                item
                for item in items
                if item["revision"] == revision
                and item["replicaSetUid"] == rs_uid
            ),
            None,
        )
        if match is None or not match["canRollback"]:
            raise HTTPException(
                409,
                (
                    match["reason"]
                    if match
                    else "来源历史已清理、变化或不属于当前 Deployment"
                ),
            )
        source = next(item for item in rs if item["metadata"]["uid"] == rs_uid)
        template = normalized_template(source["spec"]["template"])
        return template, {
            "sourceRevision": revision,
            "sourceReplicaSetUid": rs_uid,
            "sourceTemplateHash": digest(template),
        }

    def binding(self, action, body):
        return {
            "cluster": self.ops.yaml.cluster,
            "target": self.ops.target,
            "actor": policy.value(self.ops.user, "user_id"),
            "authRevision": policy.value(self.ops.user, "auth_revision"),
            "action": action,
            "parametersHash": digest(
                body.model_dump(exclude={"request_id", "preview_token"})
            ),
        }

    def prepare(self, action, body, current):
        desired = copy.deepcopy(current)
        context = {}
        if action == "image":
            self.replicasets(current)
            containers = desired["spec"]["template"]["spec"]["containers"]
            by_name = {item["name"]: item for item in containers}
            selected = set()
            for item in body.containers:
                if item.name in selected or item.name not in by_name:
                    raise HTTPException(
                        422, "容器名称未知或重复，请选择当前普通容器"
                    )
                selected.add(item.name)
                by_name[item.name]["image"] = image_reference(item.image)
        elif action == "rollback":
            desired["spec"]["template"], context = self.source(
                current, body.revision, body.replica_set_uid
            )
        else:
            raise HTTPException(422, "不支持的发布动作")
        if desired["spec"] == current["spec"]:
            raise HTTPException(409, "目标模板无变化，无需发布")
        self.ops.check(
            "resource.update",
            "deployment",
            self.ops.namespace,
            api_version="apps/v1",
            manifest=desired,
        )
        preview = patch_template(
            self.ops, current, desired["spec"]["template"], dry_run=True
        )
        # 严格预检不能偷偷改变副本数、selector 或其他非模板 spec。
        before_spec, after_spec = copy.deepcopy(
            current["spec"]
        ), copy.deepcopy(preview["spec"])
        before_spec.pop("template")
        after_spec.pop("template")
        if before_spec != after_spec:
            raise HTTPException(
                409, "准入改变了模板以外的配置，请联系管理员核实"
            )
        self.ops.check(
            "resource.update",
            "deployment",
            self.ops.namespace,
            api_version="apps/v1",
            manifest=preview,
        )
        if preview["spec"] == current["spec"]:
            raise HTTPException(409, "预检后目标模板无变化")
        context["containers"] = images(preview["spec"]["template"])
        context["changedPaths"] = [
            item["path"]
            for item in safe_changes(
                {"spec": {"template": current["spec"]["template"]}},
                {"spec": {"template": preview["spec"]["template"]}},
            )
        ]
        # requested 保留准入之前的完整模板；执行前重做 dry-run 校验结果。
        return desired, preview, context

    def preview(self, action, body):
        from core.services.deployment_operations import validate_target

        self.ops.check("resource.update", "deployment", self.ops.namespace)
        current = self.ops.read()
        validate_target(current, body.uid, body.resource_version)
        _, desired, context = self.prepare(action, body, current)
        token = (
            history_cipher()
            .encrypt(
                json.dumps(
                    {
                        "binding": self.binding(action, body),
                        "desiredHash": digest(desired["spec"]),
                        "contextHash": digest(context),
                    },
                    ensure_ascii=False,
                ).encode()
            )
            .decode()
        )
        return {
            "previewToken": token,
            "expiresAt": (
                datetime.utcnow() + timedelta(seconds=PREVIEW_SECONDS)
            ).isoformat()
            + "Z",
            "uid": body.uid,
            "resourceVersion": body.resource_version,
            "strategy": current["spec"].get("strategy", {}),
            "replicas": current["spec"].get("replicas", 1),
            "changes": safe_changes(
                {"spec": {"template": current["spec"]["template"]}},
                {"spec": {"template": desired["spec"]["template"]}},
            ),
            "sourceRevision": context.get("sourceRevision", ""),
        }

    def verify(self, action, body, current):
        try:
            token = json.loads(
                history_cipher().decrypt(
                    body.preview_token.encode(), ttl=PREVIEW_SECONDS
                )
            )
        except (InvalidToken, ValueError, UnicodeError):
            raise HTTPException(
                409, "预检凭证无效或已过期，请重新预检"
            ) from None
        if token.get("binding") != self.binding(action, body):
            raise HTTPException(409, "预检身份或输入已变化，请重新预检")
        requested, desired, context = self.prepare(action, body, current)
        if token.get("desiredHash") != digest(desired["spec"]) or token.get(
            "contextHash"
        ) != digest(context):
            raise HTTPException(409, "目标模板或来源历史已变化，请重新预检")
        return requested, desired, context

    def verify_source(self, row, current):
        if row.action == "rollback":
            _, context = self.source(
                current,
                row.release_context["sourceRevision"],
                row.release_context["sourceReplicaSetUid"],
            )
            if any(
                context[key] != row.release_context[key] for key in context
            ):
                raise HTTPException(409, "来源历史在受理后变化，未执行回滚")
