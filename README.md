# media-guard

NAS 外网媒体播放自动限速工具。

## 功能

- 自动检测外网上传流量
- 自动限速 qBittorrent / Transmission
- 外网播放结束自动恢复

## 使用

```bash
git clone https://github.com/llzg/media-guard.git
cd media-guard
docker compose up -d
```

## 配置

修改 config.yaml

## 原理

检测 NAS 外网上传流量判断是否正在外网播放媒体。
