import time
import yaml
import requests
from qbittorrentapi import Client as QBClient
from transmission_rpc import Client as TRClient

def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)

def get_tx_bytes(interface):
    with open("/proc/net/dev") as f:
        for line in f:
            if interface in line:
                return int(line.split(":")[1].split()[8])
    raise RuntimeError("网卡不存在")

def get_speed(interface, interval):
    a = get_tx_bytes(interface)
    time.sleep(interval)
    b = get_tx_bytes(interface)
    return (b - a) / interval / 1024 / 1024

def set_qb(cfg, limited):
    if not cfg["qb"]["enable"]:
        return
    qb = QBClient(host=cfg["qb"]["url"], username=cfg["qb"]["user"], password=cfg["qb"]["pass"])
    qb.auth_log_in()
    limit = cfg["qb"]["limit"] if limited else 0
    qb.transfer.set_upload_limit(limit * 1024)

def set_tr(cfg, limited):
    if not cfg["tr"]["enable"]:
        return
    tr = TRClient(host=cfg["tr"]["host"], port=cfg["tr"]["port"], username=cfg["tr"]["user"], password=cfg["tr"]["pass"])
    if limited:
        tr.session_set(speed_limit_up_enabled=True, speed_limit_up=cfg["tr"]["limit"])
    else:
        tr.session_set(speed_limit_up_enabled=False)

def main():
    cfg = load_config()
    iface = cfg["interface"]
    interval = cfg["interval"]
    up_th = cfg["trigger"]
    down_th = cfg["recover"]

    high = low = 0
    limited = False

    while True:
        try:
            sp = get_speed(iface, interval)
            print(f"上传 {sp:.2f} MB/s")

            if sp > up_th:
                high += 1
                low = 0
            elif sp < down_th:
                low += 1
                high = 0

            if not limited and high >= cfg["trigger_count"]:
                print("触发限速")
                set_qb(cfg, True)
                set_tr(cfg, True)
                limited = True

            if limited and low >= cfg["recover_count"]:
                print("恢复限速")
                set_qb(cfg, False)
                set_tr(cfg, False)
                limited = False

        except Exception as e:
            print(e)
            time.sleep(5)

if __name__ == "__main__":
    main()
