---
name: "image-resize-logo"
description: "批量调整图片尺寸为统一规格（1200x857），等比例缩放 + 白色填充边缘 + 压缩。Invoke when user asks to resize/rescale/compress logo images to a uniform size."
---

# 图片批量调整（Logo 统一尺寸）

## 目标规格

- **目标尺寸**: 1200x857（与 `redis_logo.png` 一致）
- **缩放方式**: 等比例缩放（不拉伸变形）
- **边缘填充**: 白色背景填充不足区域
- **压缩**: 去除元数据 + 最高压缩等级 + 256 色调色板

## 前提条件

需要安装 ImageMagick（`convert` 命令可用）。

## 单张图片处理命令

```bash
convert <input.png> \
  -resize 1200x857 \
  -background white \
  -gravity center \
  -extent 1200x857 \
  -strip \
  -define png:compression-level=9 \
  -colors 256 \
  <output.png>
```

## 批量处理（多张图片）

```bash
cd /path/to/logos_dir
for f in file1.png file2.png file3.png; do
  convert "$f" \
    -resize 1200x857 \
    -background white \
    -gravity center \
    -extent 1200x857 \
    -strip \
    -define png:compression-level=9 \
    -colors 256 \
    "/tmp/${f}.tmp.png" && mv "/tmp/${f}.tmp.png" "$f"
done
```

## 处理透明背景（去除白色背景，保留透明）

如果原图有白色背景需要去除，做成透明背景 PNG：

```bash
convert <input.png> \
  -alpha set \
  -fuzz 10% \
  -transparent white \
  -define png:compression-level=9 \
  -colors 256 \
  <output.png>
```

## 验证结果

```bash
identify <output.png>
ls -lh <output.png>
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `-resize 1200x857` | 等比例缩放，**不加 `!`**，避免强制拉伸变形 |
| `-background white` | 填充背景色为白色 |
| `-gravity center` | 图片居中放置 |
| `-extent 1200x857` | 画布扩展到目标尺寸 |
| `-strip` | 去除 PNG 元数据（EXIF 等） |
| `-define png:compression-level=9` | 最高 PNG 压缩等级 |
| `-colors 256` | 降色到 256 色调色板，减小文件大小 |
| `-alpha set` | 启用 Alpha 透明通道 |
| `-fuzz 10%` | 容差 10%，去除白色时过渡更自然 |
| `-transparent white` | 将白色变为透明 |