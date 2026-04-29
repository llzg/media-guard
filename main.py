#!/usr/bin/env python3
import ipaddress
import socket
import subprocess
import time

import requests
import yaml
from qbittorrentapi import Client as QBClient
from transmission_rpc import Client as TRClient


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def notify(cfg, message):
    log(message)
    nc = cfg.get("notify", {})
    if not nc.get("enable", False) or not nc.get("webhook"):
        return
    try:
        if nc.get("mode") == "wechat_robot":
            payload = {"msgtype": "text", "text": {"content": message}}
        else:
            payload = {"text": message}
        requests.post(nc["webhook"], json=payload, timeout=5)
    except Exception as e:
        log(f"通知失败: {e}")


def get_tx_bytes(interface):
    with open("/proc/net/dev", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(interface + ":"):
                return int(line.split(":", 1)[1].split()[8])
    raise RuntimeError(f"网卡不存在: {interface}")


def calc_upload_speed_mb(interface, last_tx, last_ts):
    now_ts = time.time()
    try:
        now_tx = get_tx_bytes(interface)
    except Exception:
        # 测试环境或接口不存在时兜底
        now_tx = last_tx if last_tx is not None else 0

    if last_tx is None or last_ts is None:
        return 0.0, now_tx, now_ts

    dt = max(0.001, now_ts - last_ts)
    speed = max(0, now_tx - last_tx) / dt / 1024 / 1024
    return speed, now_tx, now_ts


def is_public_ip(ip):
    try:
        return ipaddress.ip_address(ip.strip("[]")).is_global
    except Exception:
        return False


def split_host_port(peer):
    peer = peer.strip()
    if peer.startswith("["):
        end = peer.find("]")
        host = peer[1:end]
        port = peer[end + 2:] if end > 0 and len(peer) > end + 2 else ""
        return host, port
    if peer.count(":") > 1:
        host, _, port = peer.rpartition(":")
        return host, port
    if ":" in peer:
        return peer.rsplit(":", 1)
    return peer, ""


def parse_ss_connections():
    out = subprocess.check_output(["ss", "-H", "-tn", "state", "established"], text=True, timeout=3)
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        peer = parts[-1]
        rip, rport = split_host_port(peer)
        result.append((rip, rport))
    return result


def parse_proc_tcp_file(path, ipv6=False):
    result = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = f.readlines()[1:]
    except FileNotFoundError:
        return result

    for line in rows:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "01":
            continue
        remote = parts[2]
        hex_ip, hex_port = remote.split(":")
        try:
            if ipv6:
                raw = bytes.fromhex(hex_ip)
                rip = socket.inet_ntop(socket.AF_INET6, raw)
            else:
                raw = bytes.fromhex(hex_ip)
                rip = socket.inet_ntop(socket.AF_INET, raw[::-1])
            rport = str(int(hex_port, 16))
            result.append((rip, rport))
        except Exception:
            continue
    return result


def has_public_connection(cfg):
    cc = cfg.get("connection", {})
    if not cc.get("enable", True):
        return False, []

    ignore_ports = {str(p) for p in cc.get("ignore_remote_ports", [])}
    conns = []

    try:
        conns = parse_ss_connections()
    except FileNotFoundError:
        conns = parse_proc_tcp_file("/proc/net/tcp", ipv6=False) + parse_proc_tcp_file("/proc/net/tcp6", ipv6=True)
    except Exception as e:
        log(f"ss 检测失败，尝试 /proc/net/tcp: {e}")
        conns = parse_proc_tcp_file("/proc/net/tcp", ipv6=False) + parse_proc_tcp_file("/proc/net/tcp6", ipv6=True)

    hits = []
    for rip, rport in conns:
        if str(rport) in ignore_ports:
            continue
        if is_public_ip(rip):
            hits.append(rip)

    return bool(hits), sorted(set(hits))[:5]


class LimitController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.qb = None
        self.tr = None

    def set_qb(self, limited):
        qbcfg = self.cfg.get("qb", {})
        if not qbcfg.get("enable", False):
            return
        try:
            if self.qb is None:
                url = qbcfg["url"]
                # 去掉 http:// 前缀，QBClient 的 host 只要 IP:Port
                host = url.replace("http://", "").replace("https://", "")
                self.qb = QBClient(host=host, username=qbcfg["user"], password=qbcfg["pass"])
                self.qb.auth_log_in()
            limit = qbcfg["limit"] if limited else qbcfg.get("normal", 0)
            self.qb.transfer.set_upload_limit(int(limit) * 1024)
            log(f"qBittorrent 上传限速: {limit} KB/s")
        except Exception as e:
            import traceback
            self.qb = None
            log(f"qBittorrent 设置失败: {e}")
            log(f"详细: {traceback.format_exc()}")

    def set_tr(self, limited):
        trcfg = self.cfg.get("tr", {})
        if not trcfg.get("enable", False):
            return
        try:
            if self.tr is None:
                self.tr = TRClient(host=trcfg["host"], port=trcfg["port"], username=trcfg["user"], password=trcfg["pass"])
            limit = trcfg["limit"] if limited else trcfg.get("normal", 0)
            if int(limit) <= 0:
                self.tr.set_session(speed_limit_up_enabled=False)
                log("Transmission 上传限速: 不限速")
            else:
                self.tr.set_session(speed_limit_up_enabled=True, speed_limit_up=int(limit))
                log(f"Transmission 上传限速: {limit} KB/s")
        except Exception as e:
            self.tr = None
            log(f"Transmission 设置失败: {e}")

    def apply(self, limited):
        self.set_qb(limited)
        self.set_tr(limited)


def main():
    cfg = load_config()
    iface = cfg.get("interface", "ovs_eth1")
    interval = float(cfg.get("interval", 1))
    trigger = float(cfg.get("trigger", 2.0))
    recover = float(cfg.get("recover", 0.3))
    trigger_count_need = int(cfg.get("trigger_count", 2))
    recover_count_need = int(cfg.get("recover_count", 60))
    connection_hold = int(cfg.get("connection_hold_seconds", 60))

    controller = LimitController(cfg)
    limited = False
    high_count = 0
    low_count = 0
    last_public_conn_ts = 0
    last_reason = ""
    last_tx = None
    last_ts = None

    log("media-guard started")
    log(f"interface={iface}, interval={interval}s, trigger={trigger}MB/s, recover={recover}MB/s")

    while True:
        loop_start = time.time()
        try:
            has_conn, ips = has_public_connection(cfg)
            now = time.time()
            if has_conn:
                last_public_conn_ts = now
                # 不再立即限速，只记录供参考
                if not limited and cfg.get("connection", {}).get("immediate_limit", True) and last_public_conn_ts - now < 2:
                    log(f"检测到公网连接: {', '.join(ips)} (仅记录，不限速)")

            speed, last_tx, last_ts = calc_upload_speed_mb(iface, last_tx, last_ts)
            log(f"当前上传: {speed:.2f} MB/s, 公网连接={has_conn}, limited={limited}")

            if speed >= trigger:
                high_count += 1
                low_count = 0
            elif speed <= recover:
                low_count += 1
                high_count = 0
            else:
                high_count = 0
                low_count = 0

            if not limited and high_count >= trigger_count_need:
                last_reason = f"外网上传持续超过 {trigger} MB/s"
                controller.apply(True)
                limited = True
                notify(cfg, f"{last_reason}，已自动限速 qBittorrent / Transmission")

            conn_recent = (now - last_public_conn_ts) <= connection_hold
            # 恢复只看上传速率，不看公网连接了（TR 一直在做种）
            if limited and low_count >= recover_count_need:
                controller.apply(False)
                limited = False
                notify(cfg, f"外网访问结束，已恢复上传速度。上次触发原因：{last_reason}")
                last_reason = ""
                high_count = 0
                low_count = 0

        except Exception as e:
            log(f"运行异常: {e}")

        elapsed = time.time() - loop_start
        time.sleep(max(0.1, interval - elapsed))


if __name__ == "__main__":
    main()
