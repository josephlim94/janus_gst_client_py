
from janus_client import JanusPlugin
import asyncio

import gi
gi.require_version('GLib', '2.0')
gi.require_version('GObject', '2.0')
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

# Set to False to send H.264
DO_VP8 = True
# Set to False to disable RTX (lost packet retransmission)
DO_RTX = True
# Choose the video source:
# VIDEO_SRC="videotestsrc pattern=ball"
VIDEO_SRC="v4l2src"

if DO_VP8:
    ( encoder, payloader, rtp_encoding) = ( "vp8enc target-bitrate=100000 overshoot=25 undershoot=100 deadline=33000 keyframe-max-dist=1", "rtpvp8pay picture-id-mode=2", "VP8" )
else:
    ( encoder, payloader, rtp_encoding) = ( "x264enc", "rtph264pay aggregate-mode=zero-latency", "H264" )

PIPELINE_DESC = '''
 webrtcbin name=sendrecv stun-server=stun://stun.l.google.com:19302
 {} ! video/x-raw,width=640,height=480 ! videoconvert ! queue !
 {} ! {} !  queue ! application/x-rtp,media=video,encoding-name={},payload=96 ! sendrecv.
'''.format(VIDEO_SRC, encoder, payloader, rtp_encoding)

class JanusVideoRoomPlugin(JanusPlugin):
    name = "janus.plugin.videoroom"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.joined_event = asyncio.Event()
        self.gst_webrtc_ready = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def handle_async_response(self, response):
        if response["janus"] == "event":
            print("Event:", response)
            if "plugindata" in response:
                if response["plugindata"]["data"]["videoroom"] == "attached":
                    # Subscriber attached
                    self.joined_event.set()
                elif response["plugindata"]["data"]["videoroom"] == "joined":
                    # Participant joined (joined as publisher but may not publish)
                    self.joined_event.set()
        else:
            print("Unimplemented response handle:", response["janus"])
            print(response)
        # Handle JSEP
        if "jsep" in response:
            # print("Got JSEP:", response["jsep"])
            asyncio.create_task(self.handle_sdp(response["jsep"]))

    async def join(self, room_id, publisher_id, display_name):
        await self.send({
            "janus": "message",
            "body": {
                "request": "join",
                "ptype" : "publisher",
                "room": room_id,
                "id": publisher_id,
                "display": display_name,
            },
        })
        await self.joined_event.wait()

    async def publish(self):
        # Initialize Gst WebRTC
        self.start_pipeline()
        await self.gst_webrtc_ready.wait()
        # Create offer
        promise = Gst.Promise.new()
        self.webrtc.emit('create-offer', None, promise)
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        # Set local description
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()

        text = offer.sdp.as_text()
        print ('Sending offer and publishing:\n%s' % text)
        await self.send({
            "janus": "message",
            "body": {
                "request": "publish",
                "audio": True,
                "video": True,
            },
            "jsep": {
                'sdp': text,
                'type': 'offer',
                'trickle': True,
            }
        })
        await self.joined_event.wait()

    async def unpublish(self):
        print("Set pipeline to null")
        self.pipe.set_state(Gst.State.NULL)
        print("Set pipeline complete")
        await self.send({
            "janus": "message",
            "body": {
                "request": "unpublish",
            }
        })
        self.gst_webrtc_ready.clear()

    async def subscribe(self, room_id, feed_id):
        await self.send({
            "janus": "message",
            "body": {
                "request": "join",
                "ptype" : "subscriber",
                "room": room_id,
                "feed": feed_id,
                # "close_pc": True,
                # "audio": True,
                # "video": True,
                # "data": True,
                # "offer_audio": True,
                # "offer_video": True,
                # "offer_data": True,
            }
        })
        await self.joined_event.wait()

    async def unsubscribe(self):
        await self.send({
            "janus": "message",
            "body": {
                "request": "leave",
            }
        })
        self.joined_event.clear()

    async def list_participants(self, room_id) -> list:
        response = await self.send({
            "janus": "message",
            "body": {
                "request": "listparticipants",
                "room": room_id,
            }
        })
        return response["plugindata"]["data"]["participants"]

    def on_negotiation_needed(self, element):
        self.gst_webrtc_ready.set()
        # promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        # element.emit('create-offer', None, promise)

    def send_ice_candidate_message(self, _, sdpMLineIndex, candidate):
        # icemsg = {'candidate': candidate, 'sdpMLineIndex': mlineindex}
        # print ("Sending ICE", icemsg)
        # loop = asyncio.new_event_loop()
        future = asyncio.run_coroutine_threadsafe(self.trickle(sdpMLineIndex, candidate), self.loop)
        future.result()

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        name = caps.to_string()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q)
            self.pipe.add(conv)
            self.pipe.add(resample)
            self.pipe.add(sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def start_pipeline(self):
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('on-ice-candidate', self.send_ice_candidate_message)
        self.webrtc.connect('pad-added', self.on_incoming_stream)

        trans = self.webrtc.emit('get-transceiver', 0)
        if DO_RTX:
            trans.set_property ('do-nack', True)
        self.pipe.set_state(Gst.State.PLAYING)

    def extract_ice_from_sdp(self, sdp):
        mlineindex = -1
        for line in sdp.splitlines():
            if line.startswith("a=candidate"):
                candidate = line[2:]
                if mlineindex < 0:
                    print("Received ice candidate in SDP before any m= line")
                    continue
                print ('Received remote ice-candidate mlineindex {}: {}'.format(mlineindex, candidate))
                self.webrtc.emit('add-ice-candidate', mlineindex, candidate)
            elif line.startswith("m="):
                mlineindex += 1

    async def handle_sdp(self, msg):
        print (msg)
        if 'sdp' in msg:
            sdp = msg['sdp']
            assert(msg['type'] == 'answer')
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)

            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()

            # Extract ICE candidates from the SDP to work around a GStreamer
            # limitation in (at least) 1.16.2 and below
            self.extract_ice_from_sdp (sdp)

        elif 'ice' in msg:
            ice = msg['ice']
            candidate = ice['candidate']
            sdpmlineindex = ice['sdpMLineIndex']
            self.webrtc.emit('add-ice-candidate', sdpmlineindex, candidate)