#!/usr/bin/env python3
"""
media-guard — Modbus 读写 & 趋势监控系统
=========================================
支持 Modbus TCP 连接、地址自动换算、手动读写、
多标签实时轮询、WebSocket 推送趋势图。
"""

import os, json, time, struct, threading, re, logging
from datetime import datetime
from pathlib import Path

import yaml
from pymodbus.client import ModbusTcpClient
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

# ── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("media-guard")

# ── 加载配置 ──────────────────────────────────────────────────────────
def load_config():
    path = Path(__file__).parent / "config.yaml"
    defaults = {
        "server": {"host": "0.0.0.0", "port": 5000, "debug": False},
        "modbus": {"timeout": 5, "retries": 3},
        "trend": {"max_points": 200, "poll_interval": 1.0},
    }
    if path.exists():
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        for section, vals in defaults.items():
            if section not in cfg or not isinstance(cfg[section], dict):
                cfg[section] = vals
            else:
                for k, v in vals.items():
                    cfg[section].setdefault(k, v)
        return cfg
    return defaults

cfg = load_config()

# ── Flask 应用 ────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = os.urandom(16).hex()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── 全局状态 ──────────────────────────────────────────────────────────
client = None
connected = False
polling = False
poll_thread = None
trend_data = {}       # {tag_name: [(time_str, value), ...]}
scan_tags = []        # [{name, type, address, count, format}, ...]

# ── 地址换算 ──────────────────────────────────────────────────────────
def parse_plc_address(addr_str: str):
    """
    解析 PLC 地址字符串 → (modbus_type, offset, count)
    支持:
      400001 / 4x0001 → holding_register
      300001 / 3x0001 → input_register
      0.0 / 0x0000    → coil
      1.0 / 1x0000    → discrete_input
      00000           → 默认 holding_register
    """
    addr_str = addr_str.strip().replace(" ", "")

    # 西门子风格: 400001~465535 / 300001~365535 / 0.0 / 1.5
    m = re.match(r'^([0-4])x?0*(\d{1,5})(?:\.(\d+))?$', addr_str)
    if m:
        prefix = int(m.group(1))
        raw_offset = int(m.group(2))
        sub_index = int(m.group(3)) if m.group(3) else None
        offset = raw_offset - 1 if raw_offset > 0 else 0
        type_map = {0: "coil", 1: "discrete_input", 3: "input_register", 4: "holding_register"}
        modbus_type = type_map.get(prefix, "holding_register")
        if sub_index is not None and modbus_type in ("coil", "discrete_input"):
            offset = offset * 16 + sub_index
        return modbus_type, offset, 1

    # 裸数字: 00000~65535
    m = re.match(r'^(\d{1,5})$', addr_str)
    if m:
        return "holding_register", int(m.group(1)), 1

    raise ValueError(f"无法解析地址: {addr_str}")

def format_plc_address(modbus_type: str, offset: int) -> str:
    prefix = {"coil": 0, "discrete_input": 1, "input_register": 3, "holding_register": 4}
    p = prefix.get(modbus_type, 4)
    return f"{p}x{offset + 1:05d}"

# ── Modbus 操作 ───────────────────────────────────────────────────────
def connect_modbus(host, port=502):
    global client, connected
    try:
        if client:
            client.close()
        client = ModbusTcpClient(host, port=port, timeout=cfg["modbus"]["timeout"])
        connected = client.connect()
        if connected:
            log.info(f"已连接 {host}:{port}")
        return connected
    except Exception as e:
        log.error(f"连接失败: {e}")
        connected = False
        return False

def disconnect_modbus():
    global client, connected, polling
    polling = False
    if client:
        client.close()
    connected = False
    log.info("已断开")

def read_modbus(modbus_type, address, count=1):
    if not client or not connected:
        return None, "未连接"
    for attempt in range(cfg["modbus"]["retries"]):
        try:
            if modbus_type == "coil":
                rr = client.read_coils(address, count)
            elif modbus_type == "discrete_input":
                rr = client.read_discrete_inputs(address, count)
            elif modbus_type == "input_register":
                rr = client.read_input_registers(address, count)
            elif modbus_type == "holding_register":
                rr = client.read_holding_registers(address, count)
            else:
                return None, f"未知类型: {modbus_type}"
            if rr.isError():
                raise ConnectionError(str(rr))
            return rr, None
        except Exception as e:
            if attempt < cfg["modbus"]["retries"] - 1:
                time.sleep(0.5)
                continue
            return None, str(e)

def write_modbus(modbus_type, address, value):
    if not client or not connected:
        return False, "未连接"
    try:
        if modbus_type == "coil":
            r = client.write_coil(address, bool(value))
        elif modbus_type == "holding_register":
            if isinstance(value, list):
                r = client.write_registers(address, [int(v) for v in value])
            else:
                r = client.write_register(address, int(value))
        else:
            return False, f"不支持写入 {modbus_type}"
        return (False, str(r)) if r.isError() else (True, None)
    except Exception as e:
        return False, str(e)

def decode_value(raw_data, modbus_type, fmt="raw"):
    if modbus_type in ("coil", "discrete_input"):
        bits = raw_data.bits if hasattr(raw_data, "bits") else [raw_data]
        return [int(b) for b in bits]

    registers = raw_data.registers if hasattr(raw_data, "registers") else raw_data
    if isinstance(registers, int):
        registers = [registers]

    if fmt == "raw":
        return registers if len(registers) > 1 else registers[0]
    elif fmt == "uint16":
        return registers[0]
    elif fmt == "int16":
        v = registers[0]
        return v if v < 32768 else v - 65536
    elif fmt == "int32":
        return (registers[0] << 16) | registers[1] if len(registers) >= 2 else registers[0]
    elif fmt == "float32":
        if len(registers) >= 2:
            buf = struct.pack(">HH", registers[0], registers[1])
            return round(struct.unpack(">f", buf)[0], 4)
        return registers[0]
    return registers if len(registers) > 1 else registers[0]

# ── 轮询线程 ──────────────────────────────────────────────────────────
def poll_loop():
    global polling, trend_data
    max_pts = cfg["trend"]["max_points"]
    while polling:
        for tag in scan_tags:
            try:
                rr, err = read_modbus(tag["type"], tag["address"], tag.get("count", 1))
                if err:
                    continue
                value = decode_value(rr, tag["type"], tag.get("format", "raw"))
                now = datetime.now().strftime("%H:%M:%S")
                name = tag["name"]

                if name not in trend_data:
                    trend_data[name] = []
                trend_data[name].append((now, value))
                if len(trend_data[name]) > max_pts:
                    trend_data[name] = trend_data[name][-max_pts:]

                socketio.emit("modbus_data", {
                    "name": name,
                    "value": value,
                    "time": now,
                })
            except Exception as e:
                log.error(f"轮询错误 {tag.get('name','?')}: {e}")

        for _ in range(int(10 * cfg["trend"]["poll_interval"])):
            if not polling:
                break
            time.sleep(0.1)

# ── Web 路由 ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify({"connected": connected})

@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json
    ok = connect_modbus(data["host"], int(data.get("port", 502)))
    return jsonify({"ok": ok})

@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    disconnect_modbus()
    return jsonify({"ok": True})

@app.route("/api/parse-address", methods=["POST"])
def api_parse():
    try:
        t, off, cnt = parse_plc_address(request.json["address"])
        desc = {"coil": "线圈 0x — 可读写, 位", "discrete_input": "离散输入 1x — 只读, 位",
                "input_register": "输入寄存器 3x — 只读, 16位", "holding_register": "保持寄存器 4x — 可读写, 16位"}
        return jsonify({"ok": True, "type": t, "offset": off, "count": cnt,
                        "plc_address": format_plc_address(t, off), "description": desc.get(t, "")})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/read", methods=["POST"])
def api_read():
    data = request.json
    try:
        t, off, _ = parse_plc_address(data["address"])
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    rr, err = read_modbus(t, off, int(data.get("count", 1)))
    if err:
        return jsonify({"ok": False, "error": err})
    val = decode_value(rr, t, data.get("format", "raw"))
    return jsonify({"ok": True, "value": val, "plc_address": format_plc_address(t, off)})

@app.route("/api/write", methods=["POST"])
def api_write():
    data = request.json
    try:
        t, off, _ = parse_plc_address(data["address"])
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    v = data["value"]
    ok, err = write_modbus(t, off, v)
    return jsonify({"ok": ok, "error": err})

@app.route("/api/poll/start", methods=["POST"])
def api_poll_start():
    global polling, poll_thread, scan_tags
    data = request.json
    scan_tags = []
    for tag in data.get("tags", []):
        try:
            t, off, cnt = parse_plc_address(tag["address"])
            scan_tags.append({"name": tag["name"], "type": t, "address": off,
                              "count": cnt, "format": tag.get("format", "raw")})
        except ValueError as e:
            return jsonify({"ok": False, "error": f"{tag['name']}: {e}"})
    if not polling:
        polling = True
        poll_thread = threading.Thread(target=poll_loop, daemon=True)
        poll_thread.start()
    return jsonify({"ok": True, "count": len(scan_tags)})

@app.route("/api/poll/stop", methods=["POST"])
def api_poll_stop():
    global polling
    polling = False
    return jsonify({"ok": True})

@app.route("/api/trend/all")
def api_trend_all():
    result = {}
    for name, pts in trend_data.items():
        result[name] = {"times": [p[0] for p in pts], "values": [p[1] for p in pts]}
    return jsonify(result)

# ── 入口 ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    host = cfg["server"]["host"]
    port = cfg["server"]["port"]
    debug = cfg["server"]["debug"]
    log.info(f"🚀 media-guard 启动 → http://{host}:{port}")
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
