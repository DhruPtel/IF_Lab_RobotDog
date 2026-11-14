#!/usr/bin/env python3
"""Connection test only. Sends no motion commands.

Usage: connect_test.py

Uses LocalAP with no aes_128_key: this machine must be joined to the robot's
own wifi, where the robot is always 192.168.12.1. Connects, then disconnects.
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

CONNECT_TIMEOUT_S = 60


async def main():
    print("Target: LocalAP (192.168.12.1), no AES key")

    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    try:
        await asyncio.wait_for(conn.connect(), CONNECT_TIMEOUT_S)
        print(f"RESULT: CONNECTED ip={conn.ip}")
        return 0
    except AesKeyRequiredError as e:
        print(f"RESULT: AES_KEY_REQUIRED {e}")
    except AesKeyRejectedError as e:
        print(f"RESULT: AES_KEY_REJECTED {e}")
    except RobotBusyError as e:
        print(f"RESULT: BUSY (is the app fully closed?) {e}")
    except LocalSignalingPortError as e:
        print(f"RESULT: SIGNALING_UNREACHABLE ip={conn.ip} {e}")
    except asyncio.TimeoutError:
        print(f"RESULT: TIMEOUT after {CONNECT_TIMEOUT_S}s ip={conn.ip}")
    except Exception as e:
        print(f"RESULT: OTHER_ERROR {type(e).__name__}: {e}")
    finally:
        # Always release the dog's single connection slot.
        await conn.disconnect()
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
