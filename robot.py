"""Shared Go2 plumbing for drive.py and wander.py: LocalAP connection, sport
calls, obstacle avoidance, velocity routing, raw-terminal keyboard.

Velocity routing (see drive()):
  avoidance OFF (or unknown)  -> sport Move, api 1008 on rt/api/sport/request
  avoidance ON, path "joystick" -> rt/wirelesscontroller {"lx","ly","rx","ry","keys"}
        This is the path examples/.../obstacles_avoid/obstacles_avoid.py
        uses, and its docstring says the avoidance service intercepts and
        filters it. Values are stick deflection (-1..1), NOT m/s.
  avoidance ON, path "api"    -> OBSTACLES_AVOID MOVE api 1003
        {"x","y","yaw","mode":0}, no-reply, after USE_REMOTE_COMMAND_FROM_API
        (1004) true. Documented in constants.py but not exercised by any
        library example - try it on the dog before trusting it.
"""
import asyncio
import json
import os
import random
import sys
import termios
import time

from unitree_webrtc_connect import (
    AesKeyRejectedError,
    AesKeyRequiredError,
    LocalSignalingPortError,
    RobotBusyError,
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
)
from unitree_webrtc_connect.constants import (
    DATA_CHANNEL_TYPE,
    OBSTACLES_AVOID_API,
    RTC_TOPIC,
    SPORT_CMD,
    SPORT_CMD_MCF,
)

from envfile import load_env

# ─── Configuration ───────────────────────────────────────────────────
AVOID_DRIVE_PATH = "joystick"   # "joystick" (library-demonstrated) or "api"
SPORT_RATE_HZ = 10              # sport Move / avoid-api Move publish rate
JOYSTICK_RATE_HZ = 50           # obstacles_avoid example publishes at 50 Hz
CONNECT_TIMEOUT_S = 30
REQUEST_TIMEOUT_S = 5
MAX_SPEED = 1.0                 # m/s hard cap (my sanity limit)
MAX_YAW = 1.5                   # rad/s hard cap (my sanity limit)
# ─────────────────────────────────────────────────────────────────────

SPORT_TOPIC = RTC_TOPIC["SPORT_MOD"]
AVOID_TOPIC = RTC_TOPIC["OBSTACLES_AVOID"]
JOY_TOPIC = RTC_TOPIC["WIRELESS_CONTROLLER"]


class QuitRequested(Exception):
    pass


class ConnectFailed(Exception):
    def __init__(self, tag, detail, hint):
        super().__init__(f"ERROR {tag}: {detail}\n  -> {hint}")


def clamp(v, lim):
    return max(-lim, min(lim, v))


def conn_target():
    """(mode, ip) for the connection. Precedence: --mode/--ip on the command
    line, then UNITREE_MODE/UNITREE_IP in the environment, then .env.
    mode "ap" -> LocalAP (dog hotspot), "sta" -> LocalSTA with ip=UNITREE_IP.
    Default mode is "ap"."""
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--mode", choices=("ap", "sta"))
    p.add_argument("--ip")
    args, _ = p.parse_known_args()
    env = load_env()
    mode = (args.mode or os.environ.get("UNITREE_MODE") or env.get("UNITREE_MODE") or "ap").lower()
    ip = args.ip or os.environ.get("UNITREE_IP") or env.get("UNITREE_IP")
    if mode not in ("ap", "sta"):
        raise ConnectFailed("BAD_MODE", f"UNITREE_MODE={mode!r}", "Use ap or sta.")
    if mode == "sta" and not ip:
        raise ConnectFailed("NO_IP", "sta mode needs an IP",
                            "Set UNITREE_IP in .env or pass --ip <addr>.")
    return mode, ip


def status_code(resp):
    return resp.get("data", {}).get("header", {}).get("status", {}).get("code", -1)


class Robot:
    def __init__(self, quit_event: asyncio.Event):
        self.quit = quit_event
        self.conn = None
        self.avoid_on = None            # True / False / None (unknown)
        self.api_control = False        # 1004 handover active (api path)
        # Camera: ONE frame pump, registered right after connect. The library
        # runs video callbacks one after another and each loops forever, so
        # only the first registered callback ever sees frames. Everything
        # reads self.frame instead of registering its own callback.
        self.frame = None               # latest av.VideoFrame
        self.frame_t = None             # wall-clock time it arrived
        self.frames = 0                 # frames received this connection

    # ── plumbing ────────────────────────────────────────────────────
    async def until_quit(self, coro, timeout):
        """Await coro; raise QuitRequested on ESC/Ctrl+C, TimeoutError on timeout."""
        task = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(self.quit.wait())
        done, _ = await asyncio.wait({task, waiter}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if task in done:
            return task.result()
        task.cancel()
        if self.quit.is_set():
            raise QuitRequested()
        raise asyncio.TimeoutError()

    @property
    def connected(self):
        return self.conn is not None and getattr(self.conn, "isConnected", False)

    async def connect(self):
        mode, ip = conn_target()
        key = load_env().get("UNITREE_AES_128_KEY") or None
        target = "192.168.12.1 (dog hotspot)" if mode == "ap" else ip
        print(f"Mode: {mode}  Target: {target}  "
              f"AES key: {'from .env' if key else 'not set'}  (ESC/Ctrl+C aborts)")
        if mode == "ap":
            self.conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP,
                                                aes_128_key=key)
        else:
            self.conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA,
                                                ip=ip, aes_128_key=key)
        try:
            await self.until_quit(self.conn.connect(), CONNECT_TIMEOUT_S)
        except QuitRequested:
            raise
        except AesKeyRequiredError as e:
            raise ConnectFailed("AES_KEY_REQUIRED", e, "Firmware wants the per-device key; see RUNBOOK.")
        except AesKeyRejectedError as e:
            raise ConnectFailed("AES_KEY_REJECTED", e, "UNITREE_AES_128_KEY in .env is wrong for this robot.")
        except RobotBusyError as e:
            raise ConnectFailed("ROBOT_BUSY", e, "Close the Unitree app on every phone, then retry.")
        except LocalSignalingPortError as e:
            raise ConnectFailed("SIGNALING_UNREACHABLE", e,
                                "Are you joined to the GO2-xxxx wifi?" if mode == "ap" else
                                f"Is the dog on this network at {ip}? Its IP may have changed.")
        except asyncio.TimeoutError:
            raise ConnectFailed("TIMEOUT", f"no connection after {CONNECT_TIMEOUT_S}s",
                                "Check the GO2 wifi; power-cycle the dog if it persists.")
        except Exception as e:
            raise ConnectFailed(type(e).__name__, e, "Connection failed; see RUNBOOK.")
        self.conn.video.add_track_callback(self._frame_pump)

    async def _frame_pump(self, track):
        while True:
            self.frame = await track.recv()
            self.frame_t = time.time()
            self.frames += 1

    async def start_video(self, warmup_s=8.0):
        """Turn the video channel on and wait up to warmup_s for frames.
        Returns frames received (0 = camera not delivering)."""
        self.conn.video.switchVideoChannel(True)
        end = time.monotonic() + warmup_s
        while time.monotonic() < end and self.frames < 3:
            await self.until_quit(asyncio.sleep(0.1), None)
        return self.frames

    async def request(self, topic, api_id, parameter=None, interruptible=True,
                      timeout=REQUEST_TIMEOUT_S):
        """Request/response call as in the examples. Returns (code, data_str)."""
        payload = {"api_id": api_id}
        if parameter is not None:
            payload["parameter"] = parameter
        coro = self.conn.datachannel.pub_sub.publish_request_new(topic, payload)
        if interruptible:
            resp = await self.until_quit(coro, timeout)
        else:
            resp = await asyncio.wait_for(coro, timeout)
        return status_code(resp), resp.get("data", {}).get("data", "")

    def _noreply(self, topic, api_id, parameter):
        """Fire-and-forget, same wire shape as sportmode_mcf.call_no_reply."""
        self.conn.datachannel.pub_sub.publish_without_callback(topic, {
            "header": {
                "identity": {
                    "id": int(time.time() * 1000) % 2147483648 + random.randint(0, 1000),
                    "api_id": api_id,
                },
                "policy": {"priority": 0, "noreply": True},
            },
            "parameter": json.dumps(parameter),
            "binary": [],
        })

    def subscribe(self, topic, callback):
        self.conn.datachannel.pub_sub.subscribe(topic, callback)

    # ── sport ───────────────────────────────────────────────────────
    async def sport(self, name, parameter=None, interruptible=True):
        code, _ = await self.request(SPORT_TOPIC, SPORT_CMD[name], parameter,
                                     interruptible=interruptible)
        return code

    async def sport_mcf(self, name, parameter=None):
        """For MCF-only commands (e.g. TrotRun 1062). Returns (code, data_str)."""
        return await self.request(SPORT_TOPIC, SPORT_CMD_MCF[name], parameter)

    async def motion_mode(self):
        """Read-only motion_switcher query; None if the firmware doesn't answer."""
        try:
            code, data = await self.request(RTC_TOPIC["MOTION_SWITCHER"], 1001, timeout=3)
            return json.loads(data).get("name") if code == 0 and data else None
        except QuitRequested:
            raise
        except Exception:
            return None

    # ── obstacle avoidance ──────────────────────────────────────────
    async def get_avoid(self):
        code, data = await self.request(AVOID_TOPIC, OBSTACLES_AVOID_API["SWITCH_GET"])
        if code == 0 and data:
            try:
                return json.loads(data).get("enable")
            except Exception:
                pass
        return None

    async def set_avoid(self, enable: bool):
        """Switch avoidance, then read it back. Returns (state, set_code)."""
        code, _ = await self.request(AVOID_TOPIC, OBSTACLES_AVOID_API["SWITCH_SET"],
                                     {"enable": enable})
        try:
            state = await self.get_avoid()
        except asyncio.TimeoutError:
            state = None
        if state is None and code == 0:
            state = enable          # set accepted but no readback
        self.avoid_on = state
        if AVOID_DRIVE_PATH == "api":
            await self.set_api_control(bool(state))
        return state, code

    async def set_api_control(self, on: bool):
        if on == self.api_control:
            return
        code, _ = await self.request(
            AVOID_TOPIC, OBSTACLES_AVOID_API["USE_REMOTE_COMMAND_FROM_API"],
            {"is_remote_commands_from_api": on}, interruptible=False)
        self.api_control = on and code == 0

    # ── velocity ────────────────────────────────────────────────────
    def path_name(self):
        if self.avoid_on:
            return "avoid-joystick" if AVOID_DRIVE_PATH == "joystick" else "avoid-api"
        return "sport-move"

    def rate_hz(self):
        return JOYSTICK_RATE_HZ if self.path_name() == "avoid-joystick" else SPORT_RATE_HZ

    def drive(self, x, y, z):
        """x fwd m/s, y left m/s, z yaw-left rad/s; routed per avoidance state."""
        x, y, z = clamp(x, MAX_SPEED), clamp(y, MAX_SPEED), clamp(z, MAX_YAW)
        path = self.path_name()
        if path == "avoid-joystick":
            # obstacles_avoid example: ly+ = forward, lx+ = strafe right.
            # The example labels rx+ as "turn left", but on our dog rx+ turns
            # RIGHT (+90 asked, -86 measured, over 23 turns), so z (yaw-left)
            # is negated. Stick deflection, clamped to [-1, 1].
            self.conn.datachannel.pub_sub.publish_without_callback(JOY_TOPIC, {
                "lx": clamp(-y, 1.0), "ly": clamp(x, 1.0),
                "rx": clamp(-z, 1.0), "ry": 0.0, "keys": 0})
        elif path == "avoid-api":
            self._noreply(AVOID_TOPIC, OBSTACLES_AVOID_API["MOVE"],
                          {"x": x, "y": y, "yaw": z, "mode": 0})
        else:
            self._noreply(SPORT_TOPIC, SPORT_CMD["Move"], {"x": x, "y": y, "z": z})

    async def shutdown(self):
        """Zero velocity on every path, StopMove, release API control, disconnect."""
        if self.conn is None:
            return
        if self.connected:
            print("\nStopping...")
            try:
                self.drive(0.0, 0.0, 0.0)
                if self.avoid_on:           # also zero the sport path
                    self._noreply(SPORT_TOPIC, SPORT_CMD["Move"], {"x": 0, "y": 0, "z": 0})
                code = await self.sport("StopMove", interruptible=False)
                print(f"StopMove -> code {code}")
            except Exception as e:
                print(f"StopMove failed ({type(e).__name__}: {e}) - use the remote if he's still moving")
            try:
                await self.set_api_control(False)
            except Exception:
                pass
        await self.conn.disconnect()


# ─── keyboard ────────────────────────────────────────────────────────
def read_keys():
    """Read pending stdin bytes. Returns a list of tokens: 'ESC', 'CTRL_C',
    'EOF', or single lowercase characters. Arrow/function keys are dropped."""
    try:
        data = os.read(sys.stdin.fileno(), 64)
    except OSError:
        return []
    if not data:
        return ["EOF"]
    if data == b"\x1b":
        return ["ESC"]
    if data.startswith(b"\x1b"):
        return []
    return ["CTRL_C" if ch == "\x03" else ch
            for ch in data.decode(errors="ignore").lower()]


class RawTerminal:
    """No line buffering, no echo, Ctrl+C as a byte. Always restores."""

    def __enter__(self):
        if not sys.stdin.isatty():
            sys.exit("needs an interactive terminal")
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSADRAIN, mode)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        print()
        return False


def install_quit_handlers(loop, quit_event):
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, quit_event.set)


def run_app(coro_factory):
    """Raw terminal + asyncio.run + guaranteed restore. Exits with rc."""
    rc = 1
    with RawTerminal():
        try:
            rc = asyncio.run(coro_factory())
        except KeyboardInterrupt:
            rc = 130
    sys.exit(rc)


__all__ = ["Robot", "QuitRequested", "ConnectFailed", "read_keys", "run_app",
           "install_quit_handlers", "clamp", "DATA_CHANNEL_TYPE", "RTC_TOPIC",
           "SPORT_CMD", "SPORT_CMD_MCF"]
