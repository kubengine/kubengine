# 使用 nfs-subdir-external-provisioner 对接 NFS 存储

集群装完之后，Longhorn、local-path 这类默认存储类都绑在节点本地盘上。跨节点共享、多点同时读写（ReadWriteMany）的场景——比如业务多副本挂同一份配置、镜像仓库、备份落盘——本地盘给不了，这时候一般会把现成的 NFS 挂进来。

nfs-subdir-external-provisioner 干的就是这件事：它监听 PVC，在 NFS 共享目录下给每个 PVC 建一个子目录，再生成对应的 PV 挂到 Pod 里。NFS 服务端可以是集群内的 Pod，也可以是机房里的老存储、NAS。下面按集群内 NFS 服务端 + Helm 部署的路线走一遍。

文中的集群参数（节点 IP、存储网段）都是示例，照着做的时候替换成自己的。

## 一、先决条件

- 集群已就绪，`kubectl` 能正常连接（在 master01 上直接执行即可）。
- 所有节点已安装 `nfs-utils`（或 `nfs-common`）；Kubernetes 节点上的 kubelet 要挂 NFS 卷，靠的就是这个包里的 `mount.nfs`。CentOS / 麒麟 / openEuler 用第一条，Ubuntu / Debian 用第二条。

```bash
# RedHat 系（CentOS、openEuler、麒麟等）
yum install -y nfs-utils

# Debian 系（Ubuntu 等）
apt-get install -y nfs-common
```

批量在所有节点上装，可以直接用 kubengine 的集群执行命令（换成自己的节点 IP 列表）：

```bash
kubengine cluster exec 'yum install -y nfs-utils' \
  --hosts 172.31.96.11,172.31.96.12,172.31.96.13
```

- 部署机上有 `helm`（v3 即可，不需要 tiller）。如果集群是离线环境，把 chart 包和镜像准备好之后走离线导入，见第六节。
- 规划好 NFS 服务端的 IP 和导出路径。下面统一用 `172.31.96.20` 和 `/data/nfs`，实际按需替换。

## 二、准备 NFS 服务端

### 方式一：使用机房已有的 NFS 存储

用现成的 NFS 服务器时，跳过本节，确认两件事就够：

```bash
# 1. 服务端导出了哪些目录、对谁开放
showmount -e 172.31.96.20

# 2. 至少一台节点能手工挂上
mount -t nfs 172.31.96.20:/data/nfs /mnt && ls /mnt && umount /mnt
```

第二条通不过的话，先解决网络（存储网段是否放行 2049 端口）和导出白名单，别急着装 provisioner——装完也是 Pending。

### 方式二：在集群里起一个 NFS 服务端

集群内 NFS 一般选一个数据盘较大的节点来做，用 Deployment 把 NFS 服务端跑起来，数据目录通过 hostPath 落到宿主机上。

先在那个节点上准备目录：

```bash
mkdir -p /data/nfs
chmod 777 /data/nfs
```

然后写入 `nfs-server.yaml`：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nfs-server
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: nfs-server
  template:
    metadata:
      labels:
        app: nfs-server
    spec:
      nodeName: node01          # 固定到指定节点，换成自己的节点名
      containers:
        - name: nfs-server
          image: itsthenetwork/nfs-server-alpine:12
          securityContext:
            privileged: true
          env:
            - name: SHARED_DIRECTORY
              value: /exports
          ports:
            - name: nfs
              containerPort: 2049
            - name: mountd
              containerPort: 20048
          volumeMounts:
            - name: nfs-data
              mountPath: /exports
      volumes:
        - name: nfs-data
          hostPath:
            path: /data/nfs
            type: DirectoryOrCreate
---
apiVersion: v1
kind: Service
metadata:
  name: nfs-server
  namespace: default
spec:
  selector:
    app: nfs-server
  ports:
    - name: nfs
      port: 2049
      targetPort: 2049
    - name: mountd
      port: 20048
      targetPort: 20048
  type: ClusterIP
```

```bash
kubectl apply -f nfs-server.yaml
kubectl get pod -l app=nfs-server -o wide
```

这个镜像默认导出 `*`，也就是对所有来源开放，够用但不严谨。要收窄就在宿主机上改用系统自带的 NFS 服务端，在 `/etc/exports` 里写白名单，别用这个容器镜像。

> 关于 NFS 服务端的地址：上面给的是 ClusterIP，集群内的 provisioner 用 Service 名 `nfs-server.default.svc.cluster.local` 也能挂载。但 provisioner 的 Pod 会漂到任意节点，而节点上的 kubelet 挂卷时并不一定走集群 DNS；NFS 服务端 Pod 重建后 IP 也会变。生产环境更稳的做法是直接用宿主机 IP 加宿主机路径，也就是 `172.31.96.13:/data/nfs`（假设 NFS 服务端跑在 node01，IP 为 172.31.96.13）。如果 NFS 服务端就在集群外面，这一节整节跳过，按方式一确认连通性即可。

## 三、安装 Helm Chart

### 添加仓库并确认版本

```bash
helm repo add nfs-subdir-external-provisioner \
  https://kubernetes-sigs.github.io/nfs-subdir-external-provisioner/

helm repo update
helm search repo nfs-subdir-external-provisioner
```

`helm search` 的输出里能看到可选版本号，比如 `4.0.18`。后面所有 `--version` 都按这个填。

### 查看可配置项

不同 chart 版本的值键名有细微差别，动手之前先看一眼当前版本的全量默认值，尤其是 NFS 服务地址相关的键：

```bash
helm show values nfs-subdir-external-provisioner/nfs-subdir-external-provisioner \
  --version 4.0.18 > nfs-default-values.yaml
```

在 4.x 里 NFS 服务端由 `nfs.server` 和 `nfs.path` 两个键指定。如果输出中该键的结构与此不同（例如出现了 `nfsServer` / `nfsPath` 这类写法），以 `helm show values` 为准，把下面的参数名对应改掉即可。

### 执行安装

把参数写进 values 文件，比在命令行里堆 `--set` 好维护，出问题也容易回看。新建 `nfs-provisioner-values.yaml`：

```yaml
nfs:
  server: 172.31.96.13        # NFS 服务端地址（示例为集群内 NFS 所在节点）
  path: /data/nfs             # 导出的共享目录
  mountOptions:
    - vers=4.1                # NFS 协议版本，服务端是 NFSv3 就去掉这行或改成 vers=3
    - noresvport              # NFSv4 下建议加上，中断恢复时更稳
  volumeNamePrefix: pvc     # 子目录前缀，最终形如 pvc-<PVC的UID>-<namespace>-<PVC名>

storageClass:
  name: nfs-client            # StorageClass 名称，后面 PVC 引用它
  defaultClass: false         # 是否设为集群默认存储类；已有默认类时保持 false
  reclaimPolicy: Delete       # PVC 删除时 PV 的回收策略
  allowVolumeExpansion: true  # 允许扩容 PVC，能否真正生效取决于 NFS 服务端
  archiveOnDelete: true       # 回收时把子目录重命名为 archived-*，不直接删数据
  accessModes: ReadWriteMany  # NFS 天然支持多点读写
  volumeBindingMode: Immediate
```

```bash
helm install nfs-client-provisioner \
  nfs-subdir-external-provisioner/nfs-subdir-external-provisioner \
  --namespace nfs-provisioner \
  --create-namespace \
  --version 4.0.18 \
  -f nfs-provisioner-values.yaml
```

几个参数值得单独说一句：

- `archiveOnDelete: true` 是这套 chart 最实用的默认行为之一。删掉 PVC 后，PV 释放时子目录不会跟着消失，而是被重命名成 `archived-<原有名字>`，数据留在 NFS 目录里。手滑删了 PVC 还有得救。如果确实希望删除即释放空间，把它改成 `false`。
- `defaultClass` 只有在集群里还没有默认 StorageClass 时才设为 `true`，否则会覆盖原有默认类，影响其他业务。可以先看当前状态：

  ```bash
  kubectl get storageclass
  ```

  名称后面带 `(default)` 的那个就是当前默认类。已经有的话，这里保持 `false`，让业务在 PVC 里显式写 `storageClassName: nfs-client` 更清楚。

- `reclaimPolicy: Delete` 配合 `archiveOnDelete` 使用。若改成 `Retain`，PV 释放后不会被删除，需要人工处理，反而不方便。

## 四、验证

看 provisioner 是否正常起来：

```bash
kubectl get pod -n nfs-provisioner -o wide
kubectl get storageclass
kubectl get deployment -n nfs-provisioner
```

Pod 状态应为 `Running`。然后建一个测试 PVC，把整条链路走通。

写入 `nfs-test.yaml`：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: nfs-test-pvc
  namespace: default
spec:
  storageClassName: nfs-client
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: nfs-test-pod
  namespace: default
spec:
  containers:
    - name: test
      image: busybox:1.36
      command: ["/bin/sh", "-c", "echo hello-nfs > /data/test.txt && sleep 3600"]
      volumeMounts:
        - name: nfs-vol
          mountPath: /data
  volumes:
    - name: nfs-vol
      persistentVolumeClaim:
        claimName: nfs-test-pvc
```

```bash
kubectl apply -f nfs-test.yaml

# PVC 应很快进入 Bound
kubectl get pvc nfs-test-pvc -w
kubectl get pv | grep nfs-client

# 验证写入，并确认文件确实落在 NFS 服务端
kubectl exec nfs-test-pod -- cat /data/test.txt
```

到 NFS 服务端上看一眼子目录：

```bash
# 集群内 NFS 服务端（方式二）
ls -l /data/nfs
# 应该能看到类似 pvc-<uuid>_default_nfs-test-pvc 的目录，里面是 test.txt

# 机房已有 NFS（方式一）
showmount -e 172.31.96.20
```

验证通过后可以清理测试资源：

```bash
kubectl delete -f nfs-test.yaml
```

因为开启了 `archiveOnDelete`，NFS 目录会变成 `archived-...`，数据仍在，需要彻底清理时手工删除该目录。

## 五、在业务里使用

业务侧只要在 PVC 里指定存储类，其余和用其他 StorageClass 没有区别：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-data
  namespace: default
spec:
  storageClassName: nfs-client
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 10Gi
```

如果 Helm chart 暴露了 `storageClass`（Bitnami 系的 chart 基本都有），在安装业务时指定：

```bash
helm install my-app bitnami/xxx \
  --set persistence.storageClass=nfs-client \
  --set persistence.size=10Gi
```

有些 chart 的存储类参数留空时会用集群默认类，有些会用的自己的默认值。拿不准就显式指定 `nfs-client`，不容易出错。

## 六、离线环境部署

生产集群大多是离线的，chart 和镜像都要提前准备。这里沿用仓库里新增应用的那套做法：先准备 `<应用名>-bundles` 目录，再导入。

### 联网机器上准备 chart 和镜像

```bash
# 拉 chart 包
helm repo add nfs-subdir-external-provisioner \
  https://kubernetes-sigs.github.io/nfs-subdir-external-provisioner/
helm repo update
helm pull nfs-subdir-external-provisioner/nfs-subdir-external-provisioner \
  --version 4.0.18

# 导出镜像，版本号以 chart values 中的 image.tag 为准
mkdir -p nfs-subdir-external-provisioner-bundles
mv nfs-subdir-external-provisioner-4.0.18.tgz nfs-subdir-external-provisioner-bundles/

ctr i pull registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.18
ctr i export nfs-subdir-external-provisioner-bundles/nfs-subdir-external-provisioner.images.tar \
  registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.18
```

`import-dir` 靠文件名识别离线包，所以镜像包要按 `*.images.tar` 命名；同时它需要一个同名清单文件来知道 tar 里有哪些镜像，在 bundle 目录下写入 `nfs-subdir-external-provisioner-images.txt`：

```
registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.18
```

清单文件里一行一个镜像，和 tar 里的镜像完全对应。最后目录结构是：

```
nfs-subdir-external-provisioner-bundles/
├── nfs-subdir-external-provisioner-4.0.18.tgz
├── nfs-subdir-external-provisioner.images.tar
└── nfs-subdir-external-provisioner-images.txt
```

### 导入集群

把 `nfs-subdir-external-provisioner-bundles/` 上传到 master01：

```bash
source /opt/kubengine/env/bin/activate
kubengine-k8s sync-bitnami import-dir nfs-subdir-external-provisioner-bundles/
```

这个命令做两件事：把 chart 包 `helm push` 到 `oci://<Harbor域名>/charts`，把 tar 里的镜像按清单重建 tag 后推送到 Harbor。镜像的落点规则是 `<Harbor域名>/<原镜像仓库>/<原镜像路径>:<tag>`，所以上面那个镜像推完是 `harbor域名/registry.k8s.io/sig-storage/nfs-subdir-external-provisioner:v4.0.18`。推送过程会打印每一行，照抄输出里的完整路径即可。

之后就可以用 Harbor 里的 chart 安装：

```bash
cat >> nfs-provisioner-values.yaml << 'EOF'
image:
  repository: <Harbor域名>/registry.k8s.io/sig-storage/nfs-subdir-external-provisioner
  tag: v4.0.18
EOF

helm install nfs-client-provisioner \
  oci://<Harbor域名>/charts/nfs-subdir-external-provisioner \
  --version 4.0.18 \
  --namespace nfs-provisioner \
  --create-namespace \
  -f nfs-provisioner-values.yaml
```

有些环境不方便走 Harbor 的 OCI chart 仓库，直接用本地 chart 包装也一样：

```bash
helm install nfs-client-provisioner \
  ./nfs-subdir-external-provisioner-4.0.18.tgz \
  --namespace nfs-provisioner \
  --create-namespace \
  -f nfs-provisioner-values.yaml
```

所有节点都要能拉到镜像。`import-dir` 会配置 containerd 的透明代理，节点从 `registry.k8s.io` 拉取时会自动转发到 Harbor；不过上面 values 里显式写了 Harbor 地址，拉取路径更直接，也不依赖代理配置。两种方式都行，关键是别让 kubelet 去连外网。

## 七、常见问题

### PVC 一直 Pending

先看 PVC 事件，事件里通常直接写着原因：

```bash
kubectl describe pvc <pvc名> -n <命名空间>
```

常见原因：

- **`storageClassName` 写错或没写**。名字要和 `helm install` 时的 `storageClass.name` 完全一致。
- **provisioner Pod 没起来**。看日志：

  ```bash
  kubectl logs -n nfs-provisioner deploy/nfs-client-provisioner
  ```

- **NFS 服务端不通**。到 provisioner 所在节点上手工挂一次，把问题从集群配置拉回到网络层：

  ```bash
  mount -t nfs 172.31.96.13:/data/nfs /mnt
  ```

- **导出路径不存在或没权限**。NFS 服务端的 `/etc/exports` 里必须包含该目录，且 `exportfs -v` 能看到它。

### Pod 卡在 ContainerCreating

`kubectl describe pod` 里一般能看到 `mount failed` 之类的提示。几个高频原因：

- 节点没装 `nfs-utils`，`mount.nfs` 不存在。
- NFS 版本不匹配。服务端只开了 NFSv3，values 里却写了 `vers=4.1`，去掉这行让客户端协商即可。
- 防火墙没放行 2049、20048 端口。集群节点到 NFS 服务端之间，2049 是必须的。

### 删除 PVC 后数据还在

这是 `archiveOnDelete: true` 的预期行为，不是异常。子目录被重命名为 `archived-*` 保留下来了。确认不需要之后，手工到 NFS 服务端删除对应目录。如果确实希望删除 PVC 就释放空间，把该参数改成 `false` 再升级 release。

### 直接删了 PV / PVC 想恢复

只要 `archiveOnDelete` 为 `true`，数据目录还在，重命名回原来的名字就能再次被 provisioner 认到。目录名里带着 PVC 的 UID，改名后根据它反推比较费劲，更省事的做法是新建一个同名 PVC，UID 虽然变了，但 provisioner 会重新建目录，旧数据手工拷过去。恢复前先确认没有其他同名 PVC 正在使用。

### 想换 StorageClass 名称

改 values 里的 `storageClass.name` 后 `helm upgrade` 只会更新 StorageClass 对象，已经 Bound 的 PVC 不会改名，仍挂在旧的 PV 上。涉及改名时更稳妥的做法是新建一个 StorageClass，业务逐步迁移，旧的确认无人使用后再删。

### 查看当前 release 的配置

```bash
helm get values nfs-client-provisioner -n nfs-provisioner
helm status nfs-client-provisioner -n nfs-provisioner
```

## 八、卸载

先确认没有业务 PVC 还在用这个存储类：

```bash
kubectl get pvc -A | grep nfs-client
```

确认没有之后卸载 release：

```bash
helm uninstall nfs-client-provisioner -n nfs-provisioner
```

需要的话再删掉命名空间和残留的 StorageClass：

```bash
kubectl delete namespace nfs-provisioner
kubectl delete storageclass nfs-client
```

卸载不会动 NFS 服务端上的数据目录。那些目录要自己清理，尤其是 `archived-*` 和测试时留下的 `pvc-*`。
