# media-guard（增强版）

NAS 外网媒体访问自动限速（绿联影视 / Immich / MT Photos）

## 特性

- ⚡ 1 秒检测 + 2 秒触发限速
- 📊 外网上传流量检测（兜底）
- 📜 Lucky 日志检测（秒级响应）
- 🎯 自动识别 Immich / MT Photos
- 🔧 自动控制 qBittorrent / Transmission
- 📩 支持企业微信通知

## 启动

```bash
git pull
docker compose up -d --build
```

## 必改配置

```yaml
interface: eth0   # 改成你的外网网卡
```

## 工作逻辑

1. Lucky 日志检测到访问 → 立即限速
2. 没日志但上传持续高 → 2 秒限速
3. 上传恢复 + 无日志 → 自动恢复

## 说明

这是 NAS 场景最优解：
日志触发（快）+ 流量兜底（稳）
