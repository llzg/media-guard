#!/usr/bin/env python3
import ipaddress
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


def get_upload_speed_mb(interface, interval):
    a = get_tx_bytes(interface)
    time.sleep(interval)
    b = get_tx_bytes(interface)
    return max(0, b - a) / interval / 1024 / 1024


def is_public_ip(ip):
    try:
        obj = ipaddress.ip_address(ip.strip("[]"))
        return obj.is_global
    except Exception:
        return False


def extract_remote_ip(peer):
    peer = peer.strip()
    if peer.startswith("["):
        end = peer.find("]")
        return peer[1:end] if end > 0 else ""
    if peer.count(":") > 1:
        # IPv6 without brackets. Last colon may be port, but ipaddress handles full IPv6 poorly with port.
        return peer.rsplit(":", 1)[0]
    return peer.rsplit(":", 1)[0]


def has_public_connection(cfg):
    cc = cfg.get("connection", {})
    if not cc.get("enable", True):
        return False, []

    ignore_ports = {str(p) for p in cc.get("ignore_remote_ports", [])}
    try:
        # Try ss first, fall back to /proc/net/tcp
        try:
            out = subprocess.check_output(["ss", "-tn", "state", "established"], text=True, timeout=3)
            lines = out.splitlines()[1:]
        except FileNotFoundError:
            # ss not available (Synology), read /proc/net/tcp instead
            conns = []
            with open("/proc/net/tcp") as f:
                for line in f.readlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 10 and parts[3] == "01":  # 01 = ESTABLISHED
                        local = parts[1]
                        remote = parts[2]
                        rip = remote.rsplit(":", 1)[0]
                        # Convert hex IP to dotted decimal
                        hex_ip = rip.split(":")[0] if ":" in rip else rip
                        ip_parts = [str(int(hex_ip[i:i+2], 16)) for i in range(6, -1, -2)]
                        rip_dotted = ".".join(ip_parts)
                        rport = str(int(remote.rsplit(":", 1)[-1], 16))
                        conns.append(f"{rip_dotted}:{rport}")
                    elif len(parts) >= 10 and parts[3] == "0A":
                        pass  # 0A = LISTEN, skip
            lines = conns
    except Exception as e:
        log(f"公网连接检测失败: {e}")
        return False, []

    hits = []
    for item in lines:
        if isinstance(item, str) and ":" in item:
            # /proc/net/tcp format: "ip:port"
            rip, rport = item.rsplit(":", 1)
        elif isinstance(item, str):
            parts = item.split()
            if len(parts) < 5:
                continue
            peer = parts[4]
            rip = extract_remote_ip(peer)
            rport = peer.rsplit(":", 1)[-1]
        else:
            continue
        if rport in ignore_ports:
            continue
        if is_public_ip(rip):
            hits.append(rip)

    return bool(hits), sorted(set(hits))[:5]


def set_qb(cfg, limited):
    qbcfg = cfg.get("qb", {})
    if not qbcfg.get("enable", False):
        return
    qb = QBClient(host=qbcfg["url"], username=qbcfg["user"], password=qbcfg["pass"])
    qb.auth_log_in()
    limit = qbcfg["limit"] if limited else qbcfg.get("normal", 0)
    qb.transfer.set_upload_limit(int(limit) * 1024)
    log(f"qBittorrent 上传限速: {limit} KB/s")


def set_tr(cfg, limited):
    trcfg = cfg.get("tr", {})
    if not trcfg.get("enable", False):
        return
    tr = TRClient(host=trcfg["host"], port=trcfg["port"], username=trcfg["user"], password=trcfg["pass"])
    limit = trcfg["limit"] if limited else trcfg.get("normal", 0)
    if int(limit) <= 0:
        tr.session_set(speed_limit_up_enabled=False)
        log("Transmission 上传限速: 不限速")
    else:
        tr.session_set(speed_limit_up_enabled=True, speed_limit_up=int(limit))
        log(f"Transmission 上传限速: {limit} KB/s")


def apply_limit(cfg, limited):
    set_qb(cfg, limited)
    set_tr(cfg, limited)


def main():
    cfg = load_config()
    iface = cfg.get("interface", "ovs_eth1")
    interval = int(cfg.get("interval", 1))
    trigger = float(cfg.get("trigger", 2.0))
    recover = float(cfg.get("recover", 0.3))
    trigger_count_need = int(cfg.get("trigger_count", 2))
    recover_count_need = int(cfg.get("recover_count", 60))
    connection_hold = int(cfg.get("connection_hold_seconds", 60))

    limited = False
    high_count = 0
    low_count = 0
    last_public_conn_ts = 0
    last_reason = ""

    log("media-guard started")
    log(f"interface={iface}, interval={interval}s, trigger={trigger}MB/s, recover={recover}MB/s")

    while True:
        try:
            has_conn, ips = has_public_connection(cfg)
            now = time.time()
            if has_conn:
                last_public_conn_ts = now
                if cfg.get("connection", {}).get("immediate_limit", True) and not limited:
                    last_reason = f"检测到公网连接: {', '.join(ips)}"
                    apply_limit(cfg, True)
                    limited = True
                    notify(cfg, f"{last_reason}，已立即限速 qBittorrent / Transmission")

            speed = get_upload_speed_mb(iface, interval)
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
                apply_limit(cfg, True)
                limited = True
                notify(cfg, f"{last_reason}，已自动限速 qBittorrent / Transmission")

            conn_recent = (now - last_public_conn_ts) <= connection_hold
            if limited and low_count >= recover_count_need and not conn_recent:
                apply_limit(cfg, False)
                limited = False
                notify(cfg, f"外网访问结束，已恢复上传速度。上次触发原因：{last_reason}")
                last_reason = ""
                high_count = 0
                low_count = 0

        except Exception as e:
            log(f"运行异常: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
