#!/usr/bin/env python3
"""Step 2: connection test only. Sends NO motion commands.

Usage:
  connect.py sta-sn          LocalSTA, multicast discovery by serial + AES key
  connect.py sta-ip <IP>     LocalSTA, direct IP (+ AES key if present)
  connect.py ap              LocalAP, laptop joined to the GO2-xxxx hotspot

Connects, prints firmware / ip, then disconnects.
"""
import asyncio
import sys

from unitree_webrtc_connect import (
    AesKeyRejectedError,
    AesKeyRequiredError,
    LocalSignalingPortError,
    RobotBusyError,
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
)

from envfile import load_env

CONNECT_TIMEOUT_S = 60


def build_conn(method: str, args: list, env: dict) -> UnitreeWebRTCConnection:
    key = env.get("UNITREE_AES_128_KEY") or None
    sn = env.get("UNITREE_SERIAL") or None
    if method == "sta-sn":
        if not sn:
            sys.exit("UNITREE_SERIAL missing from .env (run get_key.py)")
        return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA,
                                       serialNumber=sn, aes_128_key=key)
    if method == "sta-ip":
        if not args:
            sys.exit("usage: connect.py sta-ip <IP>")
        return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA,
                                       ip=args[0], aes_128_key=key)
    if method == "ap":
        return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP,
                                       aes_128_key=key)
    sys.exit(__doc__)


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    method, rest = sys.argv[1], sys.argv[2:]
    env = load_env()
    print(f"Method: {method}  key_in_env={'yes' if env.get('UNITREE_AES_128_KEY') else 'no'}")
    conn = build_conn(method, rest, env)
    try:
        await asyncio.wait_for(conn.connect(), CONNECT_TIMEOUT_S)
        print(f"RESULT: CONNECTED ip={conn.ip}")
        return 0
    except AesKeyRequiredError as e:
        print(f"RESULT: AES_KEY_REQUIRED {e}")
    except AesKeyRejectedError as e:
        print(f"RESULT: AES_KEY_REJECTED {e}")
    except RobotBusyError as e:
        print(f"RESULT: BUSY (is the Unitree app fully closed?) {e}")
    except LocalSignalingPortError as e:
        print(f"RESULT: SIGNALING_UNREACHABLE ip={conn.ip} {e}")
    except asyncio.TimeoutError:
        print(f"RESULT: TIMEOUT after {CONNECT_TIMEOUT_S}s ip={conn.ip}")
    except Exception as e:
        print(f"RESULT: OTHER_ERROR {type(e).__name__}: {e}")
    finally:
        await conn.disconnect()  # always free the robot's single WebRTC slot
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
