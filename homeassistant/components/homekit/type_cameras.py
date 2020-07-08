"""Class to hold all camera accessories."""
import asyncio
from datetime import timedelta
import logging

from haffmpeg.core import HAFFmpeg
from pyhap.camera import (
    STREAMING_STATUS,
    VIDEO_CODEC_PARAM_LEVEL_TYPES,
    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES,
    Camera as PyhapCamera,
)
from pyhap.const import CATEGORY_CAMERA

from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.const import STATE_ON
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change,
    async_track_time_interval,
)
from homeassistant.util import get_local_ip

from .accessories import TYPES, HomeAccessory
from .const import (
    CHAR_MOTION_DETECTED,
    CHAR_STREAMING_STRATUS,
    CONF_AUDIO_CODEC,
    CONF_AUDIO_MAP,
    CONF_AUDIO_PACKET_SIZE,
    CONF_LINKED_MOTION_SENSOR,
    CONF_MAX_FPS,
    CONF_MAX_HEIGHT,
    CONF_MAX_WIDTH,
    CONF_STREAM_ADDRESS,
    CONF_STREAM_SOURCE,
    CONF_SUPPORT_AUDIO,
    CONF_VIDEO_CODEC,
    CONF_VIDEO_MAP,
    CONF_VIDEO_PACKET_SIZE,
    DEFAULT_AUDIO_CODEC,
    DEFAULT_AUDIO_MAP,
    DEFAULT_AUDIO_PACKET_SIZE,
    DEFAULT_MAX_FPS,
    DEFAULT_MAX_HEIGHT,
    DEFAULT_MAX_WIDTH,
    DEFAULT_SUPPORT_AUDIO,
    DEFAULT_VIDEO_CODEC,
    DEFAULT_VIDEO_MAP,
    DEFAULT_VIDEO_PACKET_SIZE,
    SERV_CAMERA_RTP_STREAM_MANAGEMENT,
    SERV_MOTION_SENSOR,
)
from .img_util import scale_jpeg_camera_image
from .util import pid_is_alive

_LOGGER = logging.getLogger(__name__)


VIDEO_OUTPUT = (
    "-map {v_map} -an "
    "-c:v {v_codec} "
    "{v_profile}"
    "-tune zerolatency -pix_fmt yuv420p "
    "-r {fps} "
    "-b:v {v_max_bitrate}k -bufsize {v_bufsize}k -maxrate {v_max_bitrate}k "
    "-payload_type 99 "
    "-ssrc {v_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key} "
    "srtp://{address}:{v_port}?rtcpport={v_port}&"
    "localrtcpport={v_port}&pkt_size={v_pkt_size}"
)

AUDIO_OUTPUT = (
    "-map {a_map} -vn "
    "-c:a {a_encoder} "
    "{a_application}"
    "-ac 1 -ar {a_sample_rate}k "
    "-b:a {a_max_bitrate}k -bufsize {a_bufsize}k "
    "-payload_type 110 "
    "-ssrc {a_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {a_srtp_key} "
    "srtp://{address}:{a_port}?rtcpport={a_port}&"
    "localrtcpport={a_port}&pkt_size={a_pkt_size}"
)

SLOW_RESOLUTIONS = [
    (320, 180, 15),
    (320, 240, 15),
]

RESOLUTIONS = [
    (320, 180),
    (320, 240),
    (480, 270),
    (480, 360),
    (640, 360),
    (640, 480),
    (1024, 576),
    (1024, 768),
    (1280, 720),
    (1280, 960),
    (1920, 1080),
]

VIDEO_PROFILE_NAMES = ["baseline", "main", "high"]

FFMPEG_WATCH_INTERVAL = timedelta(seconds=5)
FFMPEG_WATCHER = "ffmpeg_watcher"
FFMPEG_PID = "ffmpeg_pid"
SESSION_ID = "session_id"

CONFIG_DEFAULTS = {
    CONF_SUPPORT_AUDIO: DEFAULT_SUPPORT_AUDIO,
    CONF_MAX_WIDTH: DEFAULT_MAX_WIDTH,
    CONF_MAX_HEIGHT: DEFAULT_MAX_HEIGHT,
    CONF_MAX_FPS: DEFAULT_MAX_FPS,
    CONF_AUDIO_CODEC: DEFAULT_AUDIO_CODEC,
    CONF_AUDIO_MAP: DEFAULT_AUDIO_MAP,
    CONF_VIDEO_MAP: DEFAULT_VIDEO_MAP,
    CONF_VIDEO_CODEC: DEFAULT_VIDEO_CODEC,
    CONF_AUDIO_PACKET_SIZE: DEFAULT_AUDIO_PACKET_SIZE,
    CONF_VIDEO_PACKET_SIZE: DEFAULT_VIDEO_PACKET_SIZE,
}


@TYPES.register("Camera")
class Camera(HomeAccessory, PyhapCamera):
    """Generate a Camera accessory."""

    def __init__(self, hass, driver, name, entity_id, aid, config):
        """Initialize a Camera accessory object."""
        self._ffmpeg = hass.data[DATA_FFMPEG]
        self._cur_session = None
        for config_key in CONFIG_DEFAULTS:
            if config_key not in config:
                config[config_key] = CONFIG_DEFAULTS[config_key]

        max_fps = config[CONF_MAX_FPS]
        max_width = config[CONF_MAX_WIDTH]
        max_height = config[CONF_MAX_HEIGHT]
        resolutions = [
            (w, h, fps)
            for w, h, fps in SLOW_RESOLUTIONS
            if w <= max_width and h <= max_height and fps < max_fps
        ] + [
            (w, h, max_fps)
            for w, h in RESOLUTIONS
            if w <= max_width and h <= max_height
        ]

        video_options = {
            "codec": {
                "profiles": [
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"],
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"],
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["HIGH"],
                ],
                "levels": [
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"],
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_2"],
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"],
                ],
            },
            "resolutions": resolutions,
        }
        audio_options = {
			"codecs": [
				{"type": "OPUS", "samplerate": 24},
				{"type": "OPUS", "samplerate": 16},
			]
		}

        stream_address = config.get(CONF_STREAM_ADDRESS, get_local_ip())

        options = {
            "video": video_options,
            "audio": audio_options,
            "address": stream_address,
            "srtp": True,
        }

        super().__init__(
            hass,
            driver,
            name,
            entity_id,
            aid,
            config,
            category=CATEGORY_CAMERA,
            options=options,
        )
        self._char_motion_detected = None
        self.linked_motion_sensor = self.config.get(CONF_LINKED_MOTION_SENSOR)
        if not self.linked_motion_sensor:
            return
        state = self.hass.states.get(self.linked_motion_sensor)
        if not state:
            return
        serv_motion = self.add_preload_service(SERV_MOTION_SENSOR)
        self._char_motion_detected = serv_motion.configure_char(
            CHAR_MOTION_DETECTED, value=False
        )
        self._async_update_motion_state(None, None, state)

    async def run_handler(self):
        """Handle accessory driver started event.

        Run inside the Home Assistant event loop.
        """
        if self._char_motion_detected:
            async_track_state_change(
                self.hass, self.linked_motion_sensor, self._async_update_motion_state
            )

        await super().run_handler()

    @callback
    def _async_update_motion_state(
        self, entity_id=None, old_state=None, new_state=None
    ):
        """Handle link motion sensor state change to update HomeKit value."""
        detected = new_state.state == STATE_ON
        if self._char_motion_detected.value == detected:
            return

        self._char_motion_detected.set_value(detected)
        _LOGGER.debug(
            "%s: Set linked motion %s sensor to %d",
            self.entity_id,
            self.linked_motion_sensor,
            detected,
        )

    @callback
    def async_update_state(self, new_state):
        """Handle state change to update HomeKit value."""
        pass  # pylint: disable=unnecessary-pass

    async def _async_get_stream_source(self):
        """Find the camera stream source url."""
        stream_source = self.config.get(CONF_STREAM_SOURCE)
        if stream_source:
            return stream_source
        try:
            stream_source = await self.hass.components.camera.async_get_stream_source(
                self.entity_id
            )
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "Failed to get stream source - this could be a transient error or your camera might not be compatible with HomeKit yet"
            )
        if stream_source:
            self.config[CONF_STREAM_SOURCE] = stream_source
        return stream_source

    async def start_stream(self, session_info, stream_config):
        """Start a new stream with the given configuration."""
        _LOGGER.debug(
            "[%s] Starting stream with the following parameters: %s",
            session_info["id"],
            stream_config,
        )
        input_source = await self._async_get_stream_source()
        if not input_source:
            _LOGGER.error("Camera has no stream source")
            return False
        if "-i " not in input_source:
            input_source = "-i " + input_source
        video_profile = ""
        if self.config[CONF_VIDEO_CODEC] != "copy":
            video_profile = (
                "-profile:v "
                + VIDEO_PROFILE_NAMES[
                    int.from_bytes(stream_config["v_profile_id"], byteorder="big")
                ]
                + " "
            )
        audio_application = ""
        if self.config[CONF_AUDIO_CODEC] == "libopus":
            audio_application = "-application lowdelay "
        output_vars = stream_config.copy()
        output_vars.update(
            {
                "v_profile": video_profile,
                "v_bufsize": stream_config["v_max_bitrate"] * 4,
                "v_map": self.config[CONF_VIDEO_MAP],
                "v_pkt_size": self.config[CONF_VIDEO_PACKET_SIZE],
                "v_codec": self.config[CONF_VIDEO_CODEC],
                "a_bufsize": stream_config["a_max_bitrate"] * 4,
                "a_map": self.config[CONF_AUDIO_MAP],
                "a_pkt_size": self.config[CONF_AUDIO_PACKET_SIZE],
                "a_encoder": self.config[CONF_AUDIO_CODEC],
                "a_application": audio_application,
            }
        )
        output = VIDEO_OUTPUT.format(**output_vars)
        if self.config[CONF_SUPPORT_AUDIO]:
            output = output + " " + AUDIO_OUTPUT.format(**output_vars)
        _LOGGER.debug("FFmpeg output settings: %s", output)
        stream = HAFFmpeg(self._ffmpeg.binary, loop=self.driver.loop)
        opened = await stream.open(
            cmd=[], input_source=input_source, output=output, stdout_pipe=False
        )
        if not opened:
            _LOGGER.error("Failed to open ffmpeg stream")
            return False
        session_info["stream"] = stream
        _LOGGER.info(
            "[%s] Started stream process - PID %d",
            session_info["id"],
            stream.process.pid,
        )

        ffmpeg_watcher = async_track_time_interval(
            self.hass, self._async_ffmpeg_watch, FFMPEG_WATCH_INTERVAL
        )
        self._cur_session = {
            FFMPEG_WATCHER: ffmpeg_watcher,
            FFMPEG_PID: stream.process.pid,
            SESSION_ID: session_info["id"],
        }

        return await self._async_ffmpeg_watch(0)

    async def _async_ffmpeg_watch(self, _):
        """Check to make sure ffmpeg is still running and cleanup if not."""
        ffmpeg_pid = self._cur_session[FFMPEG_PID]
        session_id = self._cur_session[SESSION_ID]
        if pid_is_alive(ffmpeg_pid):
            return True

        _LOGGER.warning("Streaming process ended unexpectedly - PID %d", ffmpeg_pid)
        self._async_stop_ffmpeg_watch()
        self._async_set_streaming_available(session_id)
        return False

    @callback
    def _async_stop_ffmpeg_watch(self):
        """Cleanup a streaming session after stopping."""
        if not self._cur_session:
            return
        self._cur_session[FFMPEG_WATCHER]()
        self._cur_session = None

    @callback
    def _async_set_streaming_available(self, session_id):
        """Free the session so they can start another."""
        self.streaming_status = STREAMING_STATUS["AVAILABLE"]
        self.get_service(SERV_CAMERA_RTP_STREAM_MANAGEMENT).get_characteristic(
            CHAR_STREAMING_STRATUS
        ).notify()

    async def stop_stream(self, session_info):
        """Stop the stream for the given ``session_id``."""
        session_id = session_info["id"]
        stream = session_info.get("stream")
        if not stream:
            _LOGGER.debug("No stream for session ID %s", session_id)
            return

        self._async_stop_ffmpeg_watch()

        if not pid_is_alive(stream.process.pid):
            _LOGGER.info("[%s] Stream already stopped", session_id)
            return True

        for shutdown_method in ["close", "kill"]:
            _LOGGER.info("[%s] %s stream", session_id, shutdown_method)
            try:
                await getattr(stream, shutdown_method)()
                return
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "[%s] Failed to %s stream", session_id, shutdown_method
                )

    async def reconfigure_stream(self, session_info, stream_config):
        """Reconfigure the stream so that it uses the given ``stream_config``."""
        return True

    def get_snapshot(self, image_size):
        """Return a jpeg of a snapshot from the camera."""
        return scale_jpeg_camera_image(
            asyncio.run_coroutine_threadsafe(
                self.hass.components.camera.async_get_image(self.entity_id),
                self.hass.loop,
            ).result(),
            image_size["image-width"],
            image_size["image-height"],
        )
