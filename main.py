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
        payload = {"msgtype": "text", "text": {"content": message}} if nc.get("mode") == "wechat_robot" else {"text": message}
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
        now_tx = last_tx if last_tx is not None else 0
    if last_tx is None or last_ts is None:
        return 0.0, now_tx, now_ts
    dt = max(0.001, now_ts - last_ts)
    return max(0, now_tx - last_tx) / dt / 1024 / 1024, now_tx, now_ts


def is_public_ip(ip):
    try:
        return ipaddress.ip_address(ip.strip("[]")).is_global
    except Exception:
        return False


def split_host_port(addr):
    addr = addr.strip()
    if addr.startswith("["):
        end = addr.find("]")
        return addr[1:end], addr[end + 2:] if end > 0 and len(addr) > end + 2 else ""
    if addr.count(":") > 1:
        host, _, port = addr.rpartition(":")
        return host, port
    return addr.rsplit(":", 1) if ":" in addr else (addr, "")


def parse_ss_connections():
    out = subprocess.check_output(["ss", "-H", "-tn", "state", "established"], text=True, timeout=3)
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        lip, lport = split_host_port(parts[-2])
        rip, rport = split_host_port(parts[-1])
        result.append((lip, str(lport), rip, str(rport)))
    return result


def parse_proc_tcp_file(path, ipv6=False):
    result = []
    try:
        rows = open(path, "r", encoding="utf-8").readlines()[1:]
    except FileNotFoundError:
        return result
    for line in rows:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "01":
            continue
        local_hex_ip, local_hex_port = parts[1].split(":")
        remote_hex_ip, remote_hex_port = parts[2].split(":")
        try:
            if ipv6:
                lip = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(local_hex_ip))
                rip = socket.inet_ntop(socket.AF_INET6, bytes.fromhex(remote_hex_ip))
            else:
                lip = socket.inet_ntop(socket.AF_INET, bytes.fromhex(local_hex_ip)[::-1])
                rip = socket.inet_ntop(socket.AF_INET, bytes.fromhex(remote_hex_ip)[::-1])
            result.append((lip, str(int(local_hex_port, 16)), rip, str(int(remote_hex_port, 16))))
        except Exception:
            continue
    return result


def get_connections():
    try:
        return parse_ss_connections()
    except FileNotFoundError:
        return parse_proc_tcp_file("/proc/net/tcp") + parse_proc_tcp_file("/proc/net/tcp6", ipv6=True)
    except Exception as e:
        log(f"ss 检测失败，尝试 /proc/net/tcp: {e}")
        return parse_proc_tcp_file("/proc/net/tcp") + parse_proc_tcp_file("/proc/net/tcp6", ipv6=True)


def has_media_public_connection(cfg):
    cc = cfg.get("connection", {})
    if not cc.get("enable", True):
        return False, []
    monitor_local_ports = {str(p) for p in cc.get("monitor_local_ports", [])}
    if cc.get("require_monitor_ports", True) and not monitor_local_ports:
        return False, []
    ignore_remote_ports = {str(p) for p in cc.get("ignore_remote_ports", [])}
    ignore_local_ports = {str(p) for p in cc.get("ignore_local_ports", [])}
    hits = []
    for _lip, lport, rip, rport in get_connections():
        if rport in ignore_remote_ports or lport in ignore_local_ports:
            continue
        if monitor_local_ports and lport not in monitor_local_ports:
            continue
        if is_public_ip(rip):
            hits.append(f"{rip}:{rport}->:{lport}")
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
                host = qbcfg["url"].replace("http://", "").replace("https://", "")
                self.qb = QBClient(host=host, username=qbcfg["user"], password=qbcfg["pass"])
                self.qb.auth_log_in()
            limit = qbcfg["limit"] if limited else qbcfg.get("normal", 0)
            self.qb.transfer.set_upload_limit(int(limit) * 1024)
            log(f"qBittorrent 上传限速: {limit} KB/s")
        except Exception as e:
            self.qb = None
            log(f"qBittorrent 设置失败: {e}")

    def set_tr(self, limited):
        trcfg = self.cfg.get("tr", {})
        if not trcfg.get("enable", False):
            return
        try:
            if self.tr is None:
                self.tr = TRClient(host=trcfg["host"], port=int(trcfg["port"]), username=trcfg["user"], password=trcfg["pass"])
            limit = trcfg["limit"] if limited else trcfg.get("normal", 0)
            payload = {"speed_limit_up_enabled": int(limit) > 0}
            if int(limit) > 0:
                payload["speed_limit_up"] = int(limit)
            (self.tr.set_session if hasattr(self.tr, "set_session") else self.tr.session_set)(**payload)
            log(f"Transmission 上传限速: {limit if int(limit) > 0 else '不限速'} KB/s")
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
    bandwidth_fallback = cfg.get("bandwidth_fallback", {}).get("enable", False)
    conn_cfg = cfg.get("connection", {})
    confirm_seconds = float(conn_cfg.get("confirm_seconds", 3))
    require_upload_rise = conn_cfg.get("require_upload_rise", True)
    confirm_upload_mb = float(conn_cfg.get("confirm_upload_mb", 0.2))
    stale_seconds = float(conn_cfg.get("stale_seconds", 2))

    controller = LimitController(cfg)
    limited = False
    high_count = 0
    low_count = 0
    last_media_conn_ts = 0
    first_media_conn_ts = None
    last_media_hits = []
    last_reason = ""
    last_tx = None
    last_ts = None

    log("media-guard started")
    log(f"interface={iface}, interval={interval}s, trigger={trigger}MB/s, recover={recover}MB/s")

    while True:
        loop_start = time.time()
        try:
            has_conn, hits = has_media_public_connection(cfg)
            now = time.time()
            if has_conn:
                last_media_conn_ts = now
                last_media_hits = hits
                if first_media_conn_ts is None:
                    first_media_conn_ts = now
                conn_duration = now - first_media_conn_ts
            else:
                if first_media_conn_ts is not None and now - last_media_conn_ts > stale_seconds:
                    first_media_conn_ts = None
                    last_media_hits = []
                conn_duration = 0

            speed, last_tx, last_ts = calc_upload_speed_mb(iface, last_tx, last_ts)
            log(f"当前上传: {speed:.2f} MB/s, 媒体公网连接={has_conn}, 连接持续={conn_duration:.1f}s, limited={limited}")

            confirmed_conn = has_conn and conn_duration >= confirm_seconds
            upload_confirmed = (speed >= confirm_upload_mb) or not require_upload_rise
            if confirmed_conn and upload_confirmed and not limited:
                last_reason = f"公网媒体连接持续 {conn_duration:.1f}s 且上传 {speed:.2f} MB/s: {', '.join(last_media_hits)}"
                controller.apply(True)
                limited = True
                notify(cfg, f"{last_reason}，已限速 qBittorrent / Transmission")

            if speed >= trigger:
                high_count += 1
                low_count = 0
            elif speed <= recover:
                low_count += 1
                high_count = 0
            else:
                high_count = 0
                low_count = 0

            if bandwidth_fallback and not limited and high_count >= trigger_count_need:
                last_reason = f"外网上传持续超过 {trigger} MB/s"
                controller.apply(True)
                limited = True
                notify(cfg, f"{last_reason}，已自动限速 qBittorrent / Transmission")

            conn_recent = (now - last_media_conn_ts) <= connection_hold
            if limited and low_count >= recover_count_need and not conn_recent:
                controller.apply(False)
                limited = False
                notify(cfg, f"外网媒体访问结束，已恢复上传速度。上次触发原因：{last_reason}")
                last_reason = ""
                high_count = 0
                low_count = 0
                first_media_conn_ts = None
                last_media_hits = []

        except Exception as e:
            log(f"运行异常: {e}")

        elapsed = time.time() - loop_start
        time.sleep(max(0.1, interval - elapsed))


if __name__ == "__main__":
    main()
