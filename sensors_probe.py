#!/usr/bin/env python3
"""Read-only sensor probe. Sends NO motion commands.

Connects over LocalAP, turns on the lidar stream and video channel, listens
for PROBE_S seconds, and reports what actually arrived on each topic.
Answers: is the lidar point cloud / camera readable on this Go2 Air?
"""
import asyncio
import sys
import time

from robot import RTC_TOPIC, ConnectFailed, QuitRequested, Robot

PROBE_S = 10

TOPICS = {
    "lidar cloud (compressed voxel)": RTC_TOPIC["ULIDAR_ARRAY"],   # rt/utlidar/voxel_map_compressed
    "lidar state":                    RTC_TOPIC["ULIDAR_STATE"],   # rt/utlidar/lidar_state
    "lidar odom pose":                RTC_TOPIC["ROBOTODOM"],      # rt/utlidar/robot_pose
    "sport state (position)":         RTC_TOPIC["LF_SPORT_MOD_STATE"],
    "multiple state":                 RTC_TOPIC["MULTIPLE_STATE"],
}


async def main():
    quit_ev = asyncio.Event()
    robot = Robot(quit_ev)
    counts = {t: 0 for t in TOPICS.values()}
    samples = {}
    try:
        await robot.connect()
    except (ConnectFailed, QuitRequested) as e:
        print(e)
        return 1
    try:
        # Same sequence as examples/go2/data_channel/lidar/lidar_stream.py
        await robot.conn.datachannel.disableTrafficSaving(True)
        robot.conn.datachannel.set_decoder(decoder_type="native")
        robot.conn.datachannel.pub_sub.publish_without_callback(RTC_TOPIC["ULIDAR_SWITCH"], "on")
        for topic in TOPICS.values():
            def cb(msg, topic=topic):
                counts[topic] += 1
                samples.setdefault(topic, msg.get("data"))
            robot.subscribe(topic, cb)
        # Same as examples/go2/video/camera_stream/display_video_channel.py
        robot.conn.video.switchVideoChannel(True)     # frames land in robot.frame (pump)

        print(f"Listening {PROBE_S}s ...")
        await asyncio.sleep(PROBE_S)

        print("\n=== Results ===")
        for label, topic in TOPICS.items():
            n = counts[topic]
            print(f"{'YES' if n else 'no ':3}  {n:5d} msgs ({n / PROBE_S:5.1f}/s)  {label:32} {topic}")
        s = samples.get(RTC_TOPIC["ULIDAR_ARRAY"])
        if isinstance(s, dict):
            inner = s.get("data")
            pts = None
            if isinstance(inner, dict) and callable(inner.get("points")):
                try:
                    pts = len(inner["points"]())
                except Exception:
                    pts = None
            print(f"     lidar sample keys: {sorted(s.keys())}" + (f", points/frame: {pts}" if pts is not None else ""))
        n = robot.frames
        print(f"{'YES' if n else 'no ':3}  {n:5d} frames ({n / PROBE_S:5.1f}/s)  camera (WebRTC video track)"
              + (f"  {robot.frame.width}x{robot.frame.height}" if robot.frame is not None else ""))
    finally:
        try:
            # Lidar is deliberately left ON: obstacle avoidance depends on it.
            robot.conn.video.switchVideoChannel(False)
        except Exception:
            pass
        await robot.conn.disconnect()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
