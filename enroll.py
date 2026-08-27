#!/usr/bin/env python3
"""
CareKeeper - enroll.py (v1)

Hub-side enrollment for new devices: allocate a WireGuard address, stage
the agent bundle, deploy over SSH, and register the device.

Usage (on the hub):
  enroll.py new NAME [--ip 10.0.0.N]   allocate + stage (no changes yet)
  enroll.py deploy NAME --host HOST     push bundle + bring up tunnel + verify
  enroll.py list                        show the device registry
"""
import argparse
import ipaddress
import json
import os
import secrets
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
ENROLL_DIR = os.path.join(BASE, "enroll")
REGISTRY = os.path.join(STATE, "devices.json")
WG_NET = ipaddress.ip_network("10.0.0.0/28")
WG_HUB_IP = "10.0.0.1"
WG_CONF = "/etc/wireguard/wg0.conf"

BUNDLE_FILES = ["care_agent.py", "config.yaml", "brain.py"]
SYSTEMD_UNITS = {
    "care-agent.service": """[Unit]
Description=CareKeeper care agent (on-device caretaker)
After=network-online.target wg0.service
Wants=network-online.target wg0.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {path}/care_agent.py --check
""",
    "care-agent.timer": """[Unit]
Description=CareKeeper daily health check
Requires=care-agent.service

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
""",
}
SYSTEMD_UNITS_USER = {
    "care-agent.service": """[Unit]
Description=CareKeeper care agent (on-device caretaker)
After=network-online.target wg0.service
Wants=network-online.target wg0.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/carekeeper/care_agent.py --check
""",
    "care-agent.timer": """[Unit]
Description=CareKeeper daily health check
Requires=care-agent.service

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
""",
}


def load_registry() -> dict:
    if os.path.exists(REGISTRY):
        return json.load(open(REGISTRY))
    return {"devices": {}}


def save_registry(reg: dict):
    os.makedirs(STATE, exist_ok=True)
    with open(REGISTRY, "w") as f:
        json.dump(reg, f, indent=2)


def next_free_ip(reg: dict) -> str:
    used = {WG_HUB_IP}
    for name, d in reg["devices"].items():
        if d.get("wg_ip"):
            used.add(d["wg_ip"])
    for ip in WG_NET.hosts():
        if str(ip) == WG_HUB_IP:
            continue
        if str(ip) not in used:
            return str(ip)
    raise RuntimeError("WireGuard subnet exhausted")


def gen_keys() -> tuple[str, str]:
    priv = subprocess.run(["wg", "genkey"], capture_output=True,
                          text=True, check=True).stdout.strip()
    pub = subprocess.run(["wg", "pubkey"], input=priv + "\n",
                         capture_output=True, text=True, check=True).stdout.strip()
    return priv, pub


def cmd_new(args):
    reg = load_registry()
    if args.name in reg["devices"]:
        print(f"device '{args.name}' already enrolled")
        sys.exit(1)
    wg_ip = args.ip or next_free_ip(reg)
    if args.pubkey:
        # existing device: keep its own keypair + tunnel config untouched
        pub = args.pubkey
        priv = None
    else:
        priv, pub = gen_keys()
        os.makedirs(os.path.join(ENROLL_DIR, args.name), exist_ok=True)
        device_conf = f"""[Interface]
Address = {wg_ip}/28
PrivateKey = {priv}

[Peer]
PublicKey = {WG_HUB_PUB}
Endpoint = {WG_HUB_ENDPOINT}:51820
AllowedIPs = 10.0.0.0/28
PersistentKeepalive = 25
"""
        with open(os.path.join(ENROLL_DIR, args.name, "wg0.conf"), "w") as f:
            f.write(device_conf)
    reg["devices"][args.name] = {
        "wg_ip": wg_ip, "pubkey": pub, "status": "pending",
        "enrolled_at": None, "host": args.host or "",
        "tier": args.tier,
        "existing_wg": bool(args.pubkey),
    }
    save_registry(reg)
    print(f"[new] {args.name} -> {wg_ip} (tier={args.tier}, "
          f"existing_wg={bool(args.pubkey)})")
    if not args.pubkey:
        print(f"      device wg conf: {ENROLL_DIR}/{args.name}/wg0.conf")
    print("      next: enroll.py deploy", args.name, "--host <HOST>")


def cmd_deploy(args):
    reg = load_registry()
    dev = reg["devices"].get(args.name)
    if not dev:
        print(f"unknown device '{args.name}' (run enroll.py new first)")
        sys.exit(1)
    host = args.host or dev.get("host")
    if not host:
        print("need --host (or set host at enroll time)")
        sys.exit(1)
    # 1) add hub-side peer to wg0 (idempotent); skip if device has own tunnel
    if not dev.get("existing_wg") and not args.skip_wg:
        _add_hub_peer(dev["wg_ip"], dev["pubkey"])
    # 2) push the bundle
    if args.user_install:
        subprocess.run(["ssh", host, "mkdir -p ~/carekeeper"], check=True)
        subprocess.run(["scp", "-q"] +
                       [os.path.join(BASE, f) for f in BUNDLE_FILES] +
                       [f"{host}:/tmp/"], check=True)
        moved = " ".join(f"/tmp/{f}" for f in BUNDLE_FILES)
        subprocess.run(["ssh", host,
                        f"mv {moved} ~/carekeeper/ && rm -f /tmp/care-*"],
                       check=True)
        dest_conf = "~/carekeeper/config.yaml"
    else:
        subprocess.run(["ssh", host, "sudo", "mkdir", "-p", "/opt/carekeeper"],
                       check=True)
        for f in BUNDLE_FILES:
            subprocess.run(["scp", "-q", os.path.join(BASE, f),
                            f"{host}:/tmp/care-{f}"], check=True)
        subprocess.run(["ssh", host,
                        "sudo mv /tmp/care-* /opt/carekeeper/ && "
                        "sudo chown -R root:root /opt/carekeeper"], check=True)
        dest_conf = "/opt/carekeeper/config.yaml"
    # device config: set device_id + tier, and rewrite hub-specific paths
    # to the device's own home (config.yaml carries hub absolute paths)
    rhome = subprocess.run(["ssh", host, "echo -n $HOME"], capture_output=True,
                           text=True, check=True).stdout.strip()
    subprocess.run(["ssh", host,
                    f"sed -i 's/^device_id:.*/device_id: {args.name}/; "
                    f"s/^tier:.*/tier: {dev['tier']}/; "
                    f"s|/home/papichulo/rig-keeper|{rhome}/carekeeper|g' {dest_conf}"],
                   check=True)
    # 3) install wireguard conf + bring up wg0 (unless existing tunnel)
    if not args.skip_wg:
        subprocess.run(["scp", os.path.join(ENROLL_DIR, args.name, "wg0.conf"),
                        f"{host}:/tmp/wg0.conf"], check=True)
        subprocess.run(["ssh", host,
                        "sudo mv /tmp/wg0.conf /etc/wireguard/wg0.conf && "
                        "sudo systemctl enable --now wg-quick@wg0"], check=True)
    else:
        # ensure the existing tunnel is up
        subprocess.run(["ssh", host,
                        "sudo systemctl enable --now wg-quick@wg0 2>/dev/null || "
                        "sudo wg-quick up wg0 2>/dev/null || true"], check=False)
    # 4) install systemd units (user mode: user units, no sudo)
    units = SYSTEMD_UNITS_USER if args.user_install else SYSTEMD_UNITS
    if args.user_install:
        subprocess.run(["ssh", host, "mkdir -p ~/.config/systemd/user"],
                       check=True)
        for unit, body in units.items():
            subprocess.run(["ssh", host,
                            f"tee ~/.config/systemd/user/{unit} > /dev/null"],
                           input=body, text=True, check=True)
        subprocess.run(["ssh", host,
                        "systemctl --user daemon-reload && "
                        "systemctl --user enable --now care-agent.timer"],
                       check=True)
    else:
        for unit, body in units.items():
            body = body.format(path="/opt/carekeeper")
            subprocess.run(["ssh", host,
                            f"sudo tee /etc/systemd/system/{unit} > /dev/null"],
                           input=body, text=True, check=True)
        subprocess.run(["ssh", host,
                        "sudo systemctl daemon-reload && "
                        "sudo systemctl enable --now care-agent.timer"], check=True)
    # 5) verify round-trip over the tunnel
    time.sleep(3)
    check_path = "~/carekeeper/care_agent.py" if args.user_install \
        else "/opt/carekeeper/care_agent.py"
    out = subprocess.run(["ssh", host, f"python3 {check_path} --check"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print("[deploy] agent check failed:", out.stderr[-300:])
        dev["status"] = "error"
        save_registry(reg)
        sys.exit(1)
    dev["status"] = "enrolled"
    dev["enrolled_at"] = time.time()
    dev["host"] = host
    save_registry(reg)
    print(f"[deploy] {args.name} enrolled at {dev['wg_ip']} via {host}")
    print("         telemetry round-trip OK (see below)")
    print(out.stdout[:600])


def _add_hub_peer(wg_ip: str, pubkey: str):
    conf = open(WG_CONF).read()
    if pubkey in conf:
        print("[wg] peer already present")
        return
    peer = f"\n[Peer]\nPublicKey = {pubkey}\nAllowedIPs = {wg_ip}/32\nPersistentKeepalive = 25\n"
    with open(WG_CONF, "a") as f:
        f.write(peer)
    subprocess.run(["sudo", "wg", "syncconf", "wg0",
                    "<(wg-quick strip wg0)"], shell=True, check=True)
    print(f"[wg] peer {wg_ip} added to hub")


def cmd_list(args):
    reg = load_registry()
    print(f"{'NAME':<14} {'WG IP':<12} {'STATUS':<10} {'HOST':<22} TIER")
    for name, d in reg["devices"].items():
        print(f"{name:<14} {d['wg_ip']:<12} {d['status']:<10} "
              f"{d.get('host',''):<22} {d.get('tier','')}")


# runtime config: hub pubkey + endpoint read once
def _load_wg_info():
    global WG_HUB_PUB, WG_HUB_ENDPOINT
    out = subprocess.run(["sudo", "wg", "show", "wg0", "public-key"],
                         capture_output=True, text=True).stdout.strip()
    WG_HUB_PUB = out
    WG_HUB_ENDPOINT = os.environ.get("CK_HUB_ENDPOINT", "192.168.1.50")


def main():
    ap = argparse.ArgumentParser(description="CareKeeper enrollment")
    ap.add_argument("cmd", choices=["new", "deploy", "list"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--ip", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--tier", default="full")
    ap.add_argument("--pubkey", default=None,
                    help="existing device pubkey (device keeps its own tunnel)")
    ap.add_argument("--skip-wg", action="store_true",
                    help="skip wireguard changes on the device")
    ap.add_argument("--user-install", action="store_true",
                    help="install to ~/carekeeper with user units (no sudo)")
    args = ap.parse_args()

    _load_wg_info()
    if args.cmd == "new":
        if not args.name:
            ap.error("new requires NAME")
        cmd_new(args)
    elif args.cmd == "deploy":
        if not args.name:
            ap.error("deploy requires NAME")
        cmd_deploy(args)
    elif args.cmd == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
