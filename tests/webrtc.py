"""Optional smoke-test dependency: aiortc; run inside the isolated test network."""
import asyncio
from urllib.request import Request, urlopen
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration


async def main():
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    received = asyncio.Event()
    tasks = []
    @pc.on('track')
    def on_track(track):
        async def receive():
            frame = await track.recv()
            print(f'PASS: WebRTC decoded frame {frame.width}x{frame.height}', flush=True)
            received.set()
        tasks.append(asyncio.create_task(receive()))
    try:
        pc.addTransceiver('video', direction='recvonly')
        await pc.setLocalDescription(await pc.createOffer())
        request = Request('http://gateway:18880/synthetic/whep', data=pc.localDescription.sdp.encode(),
                          headers={'Content-Type': 'application/sdp'}, method='POST')
        with urlopen(request, timeout=10) as response:
            answer = response.read().decode()
            assert response.status == 201
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
        await asyncio.wait_for(received.wait(), 20)
    finally:
        await pc.close()
        for task in tasks:
            task.cancel()


asyncio.run(main())
