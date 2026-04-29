#!/usr/bin/env python3
import os
import time
from pathlib import Path

import requests
import yaml
from qbittorrentapi import Client as QBClient
from transmission_rpc import Client as TRClient


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def get_tx_bytes(interface):
    with open("/proc/net/dev", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(interface + ":"):
                return int(line.split(":", 1)[1].split()[8])
    raise RuntimeError(f"网卡不存在: {interface}")


def get_upload_speed_mb(interface, interval):
    before = get_tx_bytes(interface)
    time.sleep(interval)
    after = get_tx_bytes(interface)
    return max(0, after - before) / interval / 1024 / 1024


def tail_file(path, offset):
    p = Path(path)
    if not p.exists():
        return offset, ""
    size = p.stat().st_size
    if size < offset:
        offset = 0
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        f.seek(offset)
        data = f.read()
        return f.tell(), data


def detect_lucky(cfg, offsets):
    lucky = cfg.get("lucky", {})
    if not lucky.get("enable", False):
        return None, offsets

    logs = lucky.get("logs", []) or []
    keywords = lucky.get("keywords", {}) or {}
    ignore = [x.lower() for x in lucky.get("ignore_keywords", []) or []]

    for path in logs:
        offset = offsets.get(path, 0)
        new_offset, data = tail_file(path, offset)
        offsets[path] = new_offset
        text = data.lower()
        if not text:
            continue
        if any(x in text for x in ignore):
            continue
        for service, words in keywords.items():
            if isinstance(words, str):
                words = [words]
            if any(w.lower() in text for w in words):
                return service, offsets
    return None, offsets


def notify(cfg, message):
    notify_cfg = cfg.get("notify", {})
    log(message)
    if not notify_cfg.get("enable", False):
        return

    webhook = notify_cfg.get("webhook", "")
    if not webhook:
        return

    try:
        mode = notify_cfg.get("mode", "text")
        if mode == "wechat_robot":
            payload = {"msgtype": "text", "text": {"content": message}}
        else:
            payload = {"text": message}
        requests.post(webhook, json=payload, timeout=5)
    except Exception as e:
        log(f"通知失败: {e}")


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
    iface = cfg.get("interface", "eth0")
    interval = int(cfg.get("interval", 1))
    trigger = float(cfg.get("trigger", 0.5))
    recover = float(cfg.get("recover", 0.2))
    trigger_count_need = int(cfg.get("trigger_count", 2))
    recover_count_need = int(cfg.get("recover_count", 60))
    log_hold_seconds = int(cfg.get("log_hold_seconds", 60))

    high_count = 0
    low_count = 0
    limited = False
    last_reason = ""
    last_log_hit = 0
    lucky_offsets = {}

    log("media-guard started")
    log(f"interface={iface}, interval={interval}s, trigger={trigger}MB/s, recover={recover}MB/s")

    while True:
        try:
            service, lucky_offsets = detect_lucky(cfg, lucky_offsets)
            now = time.time()
            if service:
                last_log_hit = now
                if not limited:
                    last_reason = f"Lucky 日志检测到 {service} 外网访问"
                    apply_limit(cfg, True)
                    limited = True
                    notify(cfg, f"{last_reason}，已立即限速 qBittorrent / Transmission")

            speed = get_upload_speed_mb(iface, interval)
            log(f"当前上传: {speed:.2f} MB/s, limited={limited}")

            if speed >= trigger:
                high_count += 1
                low_count = 0
            elif speed <= recover:
                low_count += 1
                high_count = 0
            else:
                high_count = 0
                low_count = 0

            log_recent = (now - last_log_hit) <= log_hold_seconds

            if not limited and high_count >= trigger_count_need:
                last_reason = f"外网上传持续超过 {trigger} MB/s"
                apply_limit(cfg, True)
                limited = True
                notify(cfg, f"{last_reason}，已自动限速 qBittorrent / Transmission")

            if limited and low_count >= recover_count_need and not log_recent:
                apply_limit(cfg, False)
                limited = False
                notify(cfg, f"外网媒体访问结束，已恢复上传速度。上次触发原因：{last_reason}")
                last_reason = ""
                high_count = 0
                low_count = 0

        except Exception as e:
            log(f"运行异常: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
