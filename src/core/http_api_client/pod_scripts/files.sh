# Fixed script; operation and paths are positional arguments, never shell code.
set -eu
op=$1
p=$2
extra=${3:-}
fail() { printf 'KUBEENGINE_FS:%s:%s\n' "$1" "$2" >&2; exit 1; }
[ -n "$p" ] || fail 400 '路径不能为空'
case "$p" in /*) ;; *) fail 400 '必须使用绝对路径';; esac
b64() { printf '%s' "$1" | base64 | tr -d '\r\n'; }
if [ "$op" = list ]; then
  [ -d "$p" ] || fail 404 '目录不存在或不是目录'
  CDPATH= cd -P "$p" || fail 403 '无法访问目录'
  p=$(pwd -P)
  b64 "$p"; printf '\n'
  count=0
  for f in "$p"/* "$p"/.[!.]* "$p"/..?*; do
    [ -e "$f" ] || [ -L "$f" ] || continue
    count=$((count + 1))
    [ "$count" -le 2000 ] || fail 413 '目录超过 2000 项，请从更具体的子目录浏览'
    kind=other
    if [ -L "$f" ]; then kind=link; elif [ -d "$f" ]; then kind=directory; elif [ -f "$f" ]; then kind=file; fi
    meta=$(stat -c '%s %Y %a' "$f") || fail 403 '无法读取文件属性'
    b64 "${f##*/}"; printf '\t%s\t%s\n' "$kind" "$meta"
  done
  exit 0
fi
if [ "$op" = stat ] || [ "$op" = download ]; then
  [ ! -L "$p" ] || fail 400 '请使用文件实际路径，不能下载符号链接'
  [ -f "$p" ] || fail 404 '文件不存在或不是普通文件'
  [ -r "$p" ] || fail 403 '文件不可读'
  if [ "$op" = stat ]; then stat -c '%s' "$p"; else base64 "$p"; fi
  exit 0
fi
# Resolve the parent, never follow the final component during mutations.
parent=${p%/*}; name=${p##*/}
[ -n "$parent" ] || parent=/
[ -n "$name" ] && [ "$name" != . ] && [ "$name" != .. ] || fail 400 '不能操作根目录'
CDPATH= cd -P "$parent" || fail 404 '父目录不存在或不可访问'
p="./$name"
case "$op" in
  mkdir)
    [ ! -e "$p" ] && [ ! -L "$p" ] || fail 409 '目标已存在'
    mkdir "$p"
    ;;
  touch)
    (set -C; : > "$p") 2>/dev/null || fail 409 '文件已存在或无创建权限'
    ;;
  delete)
    [ -e "$p" ] || [ -L "$p" ] || fail 404 '目标不存在'
    if [ -L "$p" ]; then rm "$p"
    elif [ -d "$p" ]; then
      if [ "$extra" = true ]; then rm -rf "$p"; else rmdir "$p" || fail 409 '目录非空或不可删除，可选择递归删除'; fi
    elif [ -f "$p" ]; then rm "$p"
    else fail 400 '仅支持普通文件、目录和符号链接'; fi
    ;;
  rename)
    [ -e "$p" ] || [ -L "$p" ] || fail 404 '目标不存在'
    # A new basename only, so mv cannot enter another directory accidentally.
    case "$extra" in ''|.|..|*/*) fail 400 '新名称无效';; esac
    [ ! -e "./$extra" ] && [ ! -L "./$extra" ] || fail 409 '新名称已存在'
    mv -T "$p" "./$extra"
    ;;
  upload)
    overwrite=${4:-false}
    raw_size=${5:-0}
    [ ! -L "$p" ] || fail 400 '不能覆盖符号链接'
    [ ! -e "$p" ] || [ -f "$p" ] || fail 409 '目标不是普通文件'
    if [ -e "$p" ] && [ "$overwrite" != true ]; then fail 409 '文件已存在，请显式选择覆盖'; fi
    tmp=$(mktemp './.kubengine-upload.XXXXXXXXXX') || fail 403 '无法创建上传临时文件'
    trap 'rm -f "$tmp"' EXIT HUP INT TERM
    # Read an exact base64 byte count; Kubernetes exec v4 has no stdin half-close.
    head -c "$extra" | base64 -d > "$tmp"
    [ "$(stat -c '%s' "$tmp")" = "$raw_size" ] || fail 400 '上传数据不完整'
    if [ -f "$p" ]; then chmod "$(stat -c '%a' "$p")" "$tmp"; else chmod 644 "$tmp"; fi
    if [ "$overwrite" = true ]; then
      [ ! -L "$p" ] || fail 409 '目标已变更为符号链接'
      mv -fT "$tmp" "$p"
    else
      # Hard-link installation is atomic and refuses an existing destination.
      ln "$tmp" "$p" || fail 409 '目标已存在或不可创建'
      rm -f "$tmp"
    fi
    trap - EXIT HUP INT TERM
    ;;
  *) fail 400 '不支持的文件操作';;
esac
