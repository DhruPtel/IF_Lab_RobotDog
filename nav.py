"""Shared helpers for explore.py / hallway.py: IMU heading, closed-loop turns,
timed driving, odometry pose, latest camera frame -> JPEG.

Heading = degrees CLOCKWISE from the yaw at start() (IMU yaw from
rt/lf/sportmodestate imu_state.rpy[2], radians, CCW positive).
Pose = rt/utlidar/robot_pose data.pose.position {x, y} (odom frame), falling
back to rt/lf/sportmodestate position[0:2] if robot_pose goes quiet.
"""
import asyncio
import base64
import io
import math
import time

from robot import RTC_TOPIC, QuitRequested

TURN_SPEED = 0.5          # turn stick deflection
TURN_TOL_DEG = 5          # stop this close to the requested rotation
TURN_TIMEOUT_S = 8        # per 90 deg of requested rotation
TURN_FALLBACK_S = 2.6     # seconds per 90 deg when there's no IMU (uncalibrated)
FRAME_WIDTH = 640
STALE_S = 1.0
VIDEO_WARMUP_S = 8.0     # wait this long for the camera before the first decision


def wrap180(deg):
    return (deg + 180) % 360 - 180


class Nav:
    def __init__(self, robot, quit_event):
        self.robot = robot
        self.quit = quit_event
        self.yaw = None
        self.yaw_t = 0.0
        self.yaw0 = None
        self.heading_guess = 0.0      # only used without IMU
        self.pose = None
        self.pose_t = 0.0
        self.state_pos = None

    # ── subscriptions ───────────────────────────────────────────────
    def _on_state(self, message):
        try:
            d = message["data"]
            self.yaw = float(d["imu_state"]["rpy"][2])
            self.yaw_t = time.monotonic()
            self.state_pos = (float(d["position"][0]), float(d["position"][1]))
        except Exception:
            pass

    def _on_pose(self, message):
        try:
            p = message["data"]["pose"]["position"]
            self.pose = (float(p["x"]), float(p["y"]))
            self.pose_t = time.monotonic()
        except Exception:
            pass

    # Camera frames come from the single pump in robot.Robot (see there).
    @property
    def frame(self):
        return self.robot.frame

    @property
    def frame_t(self):
        return self.robot.frame_t

    async def start(self, video=True):
        """Subscribe, turn video on and let it warm up (up to VIDEO_WARMUP_S),
        wait for IMU/pose, set heading 0. Returns a one-line status."""
        self.robot.subscribe(RTC_TOPIC["LF_SPORT_MOD_STATE"], self._on_state)
        self.robot.subscribe(RTC_TOPIC["ROBOTODOM"], self._on_pose)
        t0 = time.monotonic()
        if video:
            await self.robot.start_video(VIDEO_WARMUP_S)
        for _ in range(30):
            if self.imu_ok() and self.pose_ok():
                break
            await self.pause(0.1)
        if self.imu_ok():
            self.yaw0 = self.yaw
        if not video:
            cam = "off"
        elif self.frame is not None:
            cam = f"OK ({self.robot.frames} frames in {time.monotonic() - t0:.1f}s, {self.frame.width}x{self.frame.height})"
        else:
            cam = f"NO FRAMES after {VIDEO_WARMUP_S:.0f}s - the dog is not sending video"
        return (f"IMU {'OK' if self.imu_ok() else 'MISSING - timed turns (uncalibrated)'}, "
                f"pose {'robot_pose' if self.pose_ok() else ('sportmodestate' if self.state_pos else 'MISSING')}, "
                f"camera {cam}")

    def camera_ok(self):
        return self.frame is not None

    # ── state ───────────────────────────────────────────────────────
    def imu_ok(self):
        return self.yaw is not None and time.monotonic() - self.yaw_t < STALE_S

    def pose_ok(self):
        return self.pose is not None and time.monotonic() - self.pose_t < STALE_S

    def heading(self):
        if self.imu_ok() and self.yaw0 is not None:
            return (-math.degrees(self.yaw - self.yaw0)) % 360
        return self.heading_guess % 360

    def position(self):
        if self.pose_ok():
            return self.pose
        return self.state_pos

    # ── motion ──────────────────────────────────────────────────────
    async def pause(self, seconds):
        """Sleep, but raise QuitRequested the moment ESC/Ctrl+C is pressed."""
        try:
            await asyncio.wait_for(self.quit.wait(), seconds)
            raise QuitRequested()
        except asyncio.TimeoutError:
            pass

    async def drive_for(self, x, z, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.robot.drive(x, 0, z)
            await self.pause(1.0 / self.robot.rate_hz())
        self.robot.drive(0, 0, 0)

    async def turn_by(self, delta):
        """Rotate `delta` degrees (positive = clockwise/right). Returns degrees
        actually turned. Progress is accumulated from successive IMU readings,
        so a 180 deg turn can't be confused about which way is 'short'."""
        if abs(delta) <= TURN_TOL_DEG:
            return 0.0
        z = -TURN_SPEED if delta > 0 else TURN_SPEED      # clockwise = negative yaw
        if not (self.imu_ok() and self.yaw0 is not None):
            await self.drive_for(0, z, abs(delta) / 90 * TURN_FALLBACK_S)
            self.heading_guess += delta
            return delta
        turned, prev = 0.0, self.heading()
        end = time.monotonic() + TURN_TIMEOUT_S * max(1.0, abs(delta) / 90)
        while time.monotonic() < end:
            h = self.heading()
            turned += wrap180(h - prev)
            prev = h
            if abs(turned) >= abs(delta) - TURN_TOL_DEG:
                break
            self.robot.drive(0, 0, z)
            await self.pause(1.0 / self.robot.rate_hz())
        self.robot.drive(0, 0, 0)
        return turned

    async def turn_to(self, target):
        """Turn the short way to `target` heading (deg clockwise from start)."""
        return await self.turn_by(wrap180(target - self.heading()))

    # ── camera ──────────────────────────────────────────────────────
    def jpeg_b64(self):
        """Latest frame as a ~640px-wide JPEG (base64), or None."""
        if self.frame is None:
            return None
        img = self.frame.to_image()
        img = img.resize((FRAME_WIDTH, round(img.height * FRAME_WIDTH / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.standard_b64encode(buf.getvalue()).decode()
