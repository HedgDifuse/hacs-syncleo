#!/usr/bin/env python3
"""Standalone tester for the RusClimate / Hommyn air conditioner.

Talks to the AC directly over the local Syncleo UDP protocol using the
integration's protocol.py -- no Home Assistant required. It performs the real
handshake, prints every message the AC pushes, and lets you send commands
interactively so you can confirm the command codes against the real device.

Requirements (once):
    pip install zeroconf cryptography

Usage:
    python test_conditioner.py <mac> <token> [--ip 192.168.1.236]

  <mac>    device MAC, 12 hex chars, e.g. e4b323c33c30
  <token>  32-char device token (share the device in the Hommyn app -> QR code
           -> decode it -> the `token=...` value)
  --ip     optional: skip part of discovery and force the device IP

Interactive commands:
    on                      turn on (restores auto mode)
    off                     turn off
    mode auto|cool|heat|dry|fan
    temp <16..30>
    fan  auto|min|low|middle|high|max
    rawfan <n>              send raw fan-speed byte n (0..7) to probe the mapping
    scanfan                 send 0,1,2,...,7 with a pause, to map each to a panel speed
    swing off|vertical|horizontal|both
    turbo on|off
    silent on|off
    eco on|off
    ion on|off
    fungus on|off           fungal-infection prevention (program 0, byte 4)
    clean on|off            self-cleaning (program 1, byte 1)
    state                   print last-known state
    quit
"""
import argparse
import importlib.util
import os
import sys
import threading
import time
from ipaddress import ip_address, IPv4Address

# --- load the integration's protocol.py without importing the HA package ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_PROTO_PATH = os.path.join(_REPO_ROOT, "custom_components", "syncleo_kettle", "protocol.py")
_spec = importlib.util.spec_from_file_location("syncleo_protocol", _PROTO_PATH)
proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proto)

MODES = {"off": 0, "auto": 1, "cool": 2, "dry": 3, "heat": 4, "fan": 5}
FAN = {"auto": 0, "low": 1, "mid": 2, "high": 3}
SWING = {
    "off": (0, 0),
    "vertical": (1, 0),
    "horizontal": (0, 1),
    "both": (1, 1),
}


def discover(mac: str, timeout: int = 10):
    """Find the device on _syncleo._udp.local and return (ip, port, pubkey)."""
    import zeroconf

    result = {}
    found = threading.Event()

    class Listener(zeroconf.ServiceListener):
        def _handle(self, zc, type_, name):
            if not name.lower().startswith(mac.lower()):
                return
            info = zc.get_service_info(type_, name)
            if not info or not info.properties:
                return
            props = {
                (k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v)
                for k, v in info.properties.items()
            }
            addrs = [ip_address(a) for a in info.addresses
                     if isinstance(ip_address(a), IPv4Address)]
            if not addrs:
                return
            result.update(
                ip=str(addrs[0]),
                port=int(info.port),
                public=props.get("public"),
                protocol=props.get("protocol"),
                vendor=props.get("vendor"),
                devtype=props.get("devtype"),
                basetype=props.get("basetype"),
            )
            found.set()

        add_service = _handle
        update_service = _handle

        def remove_service(self, zc, type_, name):
            pass

    zc = zeroconf.Zeroconf()
    browser = zeroconf.ServiceBrowser(zc, "_syncleo._udp.local.", Listener())
    try:
        if not found.wait(timeout):
            return None
        return result
    finally:
        browser.cancel()
        zc.close()


class Printer(proto.IncomingMessageListener):
    """Prints every message the device pushes; tracks a little state."""

    def __init__(self):
        self.state = {}

    def incoming_message(self, message):
        cls = type(message).__name__
        if isinstance(message, proto.ModeMessage):
            self.state["mode"] = message.pt.value
        elif isinstance(message, proto.TargetTemperatureMessage):
            self.state["target_temp"] = message.temperature
        elif isinstance(message, proto.CurrentTemperatureMessage):
            self.state["current_temp"] = message.current_temperature
        elif isinstance(message, proto.FanSpeedMessage):
            self.state["fan"] = message.speed
        elif isinstance(message, proto.ProgramDataMessage):
            data = message.program_data
            self.state["program_data"] = data.hex()
            # Track both program arrays so outgoing commands can preserve
            # the bits they don't change.
            # program 0: [0, vertical, horizontal, eco, fungus_prevention]
            # program 1: [1, self_clean, silent]
            if data and data[0] == 0:
                self.state["program0"] = list(data)
            elif data and data[0] == 1:
                self.state["program1"] = list(data)
        print(f"  << {cls}: {message}")
        return None  # let the connection ACK it


def program0(listener) -> list:
    """Last-seen program-0 array [0, vertical, horizontal, eco, fungus], padded to 5."""
    p0 = list(listener.state.get("program0") or [])
    if not p0 or p0[0] != 0:
        p0 = [0, 0, 0, 0, 0]
    while len(p0) < 5:
        p0.append(0)
    return p0


def program1(listener) -> list:
    """Last-seen program-1 array [1, self_clean, silent], padded to 3."""
    p1 = list(listener.state.get("program1") or [])
    if not p1 or p1[0] != 1:
        p1 = [1, 0, 0]
    while len(p1) < 3:
        p1.append(0)
    return p1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mac")
    ap.add_argument("token")
    ap.add_argument("--ip", default=None, help="force device IP (skip if discovery works)")
    args = ap.parse_args()

    mac = args.mac.replace(":", "").lower()
    if len(mac) != 12:
        sys.exit("MAC must be 12 hex chars")
    if len(args.token) != 32:
        sys.exit("token must be 32 hex chars")

    print(f"Discovering {mac} on _syncleo._udp.local ...")
    info = discover(mac)
    if not info:
        sys.exit("Device not found via mDNS. Check the MAC and that you're on the same LAN.")
    print(f"Found: vendor={info.get('vendor')} devtype={info.get('devtype')} "
          f"basetype={info.get('basetype')} protocol={info.get('protocol')} "
          f"at {info['ip']}:{info['port']}")

    ip = args.ip or info["ip"]
    pubkey = bytes.fromhex(info["public"])
    token = bytes.fromhex(args.token)

    listener = Printer()
    conn = proto.UDPConnection(
        addr=ip_address(ip),
        port=info["port"],
        device_pubkey=pubkey,
        device_token=token,
        conditioner=True,
    )
    conn.add_incoming_message_listener(listener)
    conn.start()

    def send(msg):
        conn.enqueue_message(proto.WrappedMessage(msg, ack=True))

    print("\nConnecting (handshake)... type 'help' for commands.\n")
    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()

            if cmd in ("quit", "exit", "q"):
                break
            elif cmd == "help":
                print(__doc__)
            elif cmd == "state":
                print("  state:", listener.state)
            elif cmd == "on":
                send(proto.ModeMessage(proto.PowerType(MODES["auto"])))
            elif cmd == "off":
                send(proto.ModeMessage(proto.PowerType(MODES["off"])))
            elif cmd == "mode" and len(parts) == 2 and parts[1] in MODES:
                send(proto.ModeMessage(proto.PowerType(MODES[parts[1]])))
            elif cmd == "temp" and len(parts) == 2:
                send(proto.TargetTemperatureMessage(int(parts[1])))
            elif cmd == "fan" and len(parts) == 2 and parts[1] in FAN:
                send(proto.FanSpeedMessage(FAN[parts[1]]))
            elif cmd == "rawfan" and len(parts) == 2:
                n = int(parts[1])
                print(f"  >> sending fan speed byte = {n}; watch the AC panel")
                send(proto.FanSpeedMessage(n))
            elif cmd == "scanfan":
                for n in range(8):
                    print(f"  >> fan byte = {n} (watch the panel)")
                    send(proto.FanSpeedMessage(n))
                    time.sleep(2.5)
            elif cmd == "swing" and len(parts) == 2 and parts[1] in SWING:
                v, h = SWING[parts[1]]
                p0 = program0(listener)
                p0[1], p0[2] = v, h
                send(proto.ProgramDataMessage(bytes(p0)))
            elif cmd == "turbo" and len(parts) == 2:
                send(proto.TurboMessage(parts[1] in ("on", "1", "true")))
            elif cmd == "ion" and len(parts) == 2:
                send(proto.IonizationMessage(parts[1] in ("on", "1", "true")))
            elif cmd == "silent" and len(parts) == 2:
                on = parts[1] in ("on", "1", "true")
                p1 = program1(listener)
                p1[2] = 1 if on else 0
                send(proto.ProgramDataMessage(bytes(p1)))
            elif cmd == "clean" and len(parts) == 2:
                on = parts[1] in ("on", "1", "true")
                p1 = program1(listener)
                p1[1] = 1 if on else 0
                send(proto.ProgramDataMessage(bytes(p1)))
            elif cmd == "eco" and len(parts) == 2:
                on = parts[1] in ("on", "1", "true")
                p0 = program0(listener)
                p0[3] = 1 if on else 0
                send(proto.ProgramDataMessage(bytes(p0)))
            elif cmd == "fungus" and len(parts) == 2:
                on = parts[1] in ("on", "1", "true")
                p0 = program0(listener)
                p0[4] = 1 if on else 0
                send(proto.ProgramDataMessage(bytes(p0)))
            else:
                print("  ? unknown command. type 'help'")
            time.sleep(0.2)
    finally:
        print("\nStopping...")
        conn.interrupted = True
        time.sleep(0.5)


if __name__ == "__main__":
    main()
