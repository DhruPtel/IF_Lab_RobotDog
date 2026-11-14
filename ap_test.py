import asyncio, os
from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod

async def main():
    key = os.environ.get("UNITREE_AES_128_KEY")
    kw = {"aes_128_key": key} if key else {}
    conn = UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP, **kw)
    try:
        await conn.connect()
        print("RESULT: CONNECTED")
    except Exception as e:
        print("RESULT: FAILED", type(e).__name__, e)

asyncio.run(main())
