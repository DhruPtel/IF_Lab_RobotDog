#!/usr/bin/env python3
"""Step 1: fetch the Go2 serial + AES-128 key from the Unitree cloud.

Reads UNITREE_EMAIL / UNITREE_PASSWORD from .env, writes UNITREE_SERIAL and
UNITREE_AES_128_KEY back to .env, then removes UNITREE_PASSWORD.
The key itself is never printed.
"""
import sys

from unitree_webrtc_connect import UnitreeCloud

from envfile import load_env, update_env


def main() -> int:
    env = load_env()
    email, password = env.get("UNITREE_EMAIL"), env.get("UNITREE_PASSWORD")
    if not email or not password:
        print("UNITREE_EMAIL and UNITREE_PASSWORD must both be set in .env")
        return 1

    cloud = UnitreeCloud(region="global")
    cloud.login_email(email, password)
    devices = cloud.list_devices()
    if not devices:
        print("Login OK but no robots are bound to this account.")
        return 1

    for d in devices:
        print(f"sn={d.sn} alias={d.alias!r} series={d.series} model={d.model} "
              f"online={d.online} key={'yes (%d chars)' % len(d.key) if d.key else 'none'}")
        # Non-secret cloud fields (firmware version usually lives here).
        safe = {k: v for k, v in d.raw.items()
                if "key" not in k.lower() and "token" not in k.lower()
                and "pass" not in k.lower()}
        print("  cloud record:", safe)

    go2s = [d for d in devices if d.series in ("Go2", "")] or devices
    if len(go2s) > 1:
        print("More than one robot found; set UNITREE_SERIAL / key manually.")
        return 1
    dev = go2s[0]

    new_vars = {"UNITREE_SERIAL": dev.sn}
    if dev.key:
        new_vars["UNITREE_AES_128_KEY"] = dev.key
    update_env(new_vars, remove=("UNITREE_PASSWORD",))
    print(f"Saved UNITREE_SERIAL={dev.sn}"
          f"{' and UNITREE_AES_128_KEY' if dev.key else ' (no key returned)'}; "
          "removed UNITREE_PASSWORD from .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
