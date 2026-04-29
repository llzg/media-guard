# media-guard

Modbus 读写 & 趋势监控系统 — 工业数据采集与实时可视化 Web 应用。

![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![Flask](https://img.shields.io/badge/Flask-3.0-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

## ✨ 功能

- 🔌 **Modbus TCP 连接管理** — 连接/断开任意 Modbus 设备
- 🧮 **地址自动换算** — 支持多种地址格式互转
- 📝 **手动读写** — 线圈、离散输入、输入/保持寄存器
- 📈 **实时趋势图** — WebSocket 推送，Chart.js 可视化
- 🎨 **暗色工业风 UI** — 适配 PC/平板
- 🐳 **Docker 一键部署** — 开箱即用

## 🚀 快速开始

### Docker 部署（推荐）

```bash
git clone https://github.com/llzg/media-guard.git
cd media-guard
docker compose up -d
```

访问 http://localhost:5000

### 本地运行

```bash
git clone https://github.com/llzg/media-guard.git
cd media-guard

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# .\venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt

# 启动
python main.py
```

## 🔧 配置

编辑 `config.yaml` 自定义设置：

```yaml
server:
  host: 0.0.0.0
  port: 5000
  debug: false

modbus:
  timeout: 5       # 连接超时（秒）
  retries: 3       # 重试次数

trend:
  max_points: 200  # 每个标签最多保留点数
  poll_interval: 1 # 轮询间隔（秒）
```

## 📖 使用指南

### 地址格式

| 格式 | 说明 | 示例 |
|------|------|------|
| `400001` | 保持寄存器 | 4x00001 |
| `0.0` | 线圈 0 位 | 0x00000 |
| `1.5` | 离散输入 5 位 | 1x00005 |
| `3x0100` | 输入寄存器 256 | 3x00100 |
| `30001` | 输入寄存器 0 | 3x00001 |

### Web 界面

1. 连接 Modbus 设备（IP:端口）
2. 添加监控标签（名称 + 地址）
3. 启动轮询，实时趋势图自动更新
4. 支持手动读写测试

## 📦 项目结构

```
media-guard/
├── main.py              # 主程序入口
├── config.yaml          # 配置文件
├── docker-compose.yml   # Docker 编排
├── Dockerfile           # Docker 构建
├── requirements.txt     # Python 依赖
├── templates/
│   └── index.html       # 前端页面
└── README.md            # 本文件
```

## 📄 许可证

MIT
