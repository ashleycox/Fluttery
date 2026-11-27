"""Coordinate hardware access and loopback safety across session windows."""

from contextlib import contextmanager
from weakref import WeakSet

LOOPBACK_SETTING = 'interfaceLoopbackPendingDisconnect'


class AudioOwnership:
    """Serialize physical stream ownership on the desktop event thread."""

    def __init__(self):
        self._frames = WeakSet()
        self._route_owner = None
        self._loopback_owner = None
        self._loopback_pending = None
        self._loopback_confirming = None
        self._microphone_waiters = {}
        self._microphone_requested = False

    def register(self, frame):
        self._frames.add(frame)
        if self._loopback_pending is None:
            self._loopback_pending = bool(frame.settings[LOOPBACK_SETTING])
        if self._loopback_pending and self._loopback_owner is None:
            self._loopback_owner = frame
            frame.audio.set_interface_loopback_interlock(True)
        # Only the owner runs the disconnect workflow. Other windows share
        # its hardware gate without reading that step as their local state.
        pending = self.owns_pending_loopback(frame)
        frame.settings = {**frame.settings, LOOPBACK_SETTING: pending}
        frame._persistent_settings = {**frame._persistent_settings, LOOPBACK_SETTING: pending}

    def unregister(self, frame):
        self._frames.discard(frame)
        self._microphone_waiters.pop(frame, None)
        if self._route_owner is frame:
            self._route_owner = None
        if self._loopback_owner is frame:
            # An OS shutdown may close the owner while its cable is still
            # connected. Keep the process gate and persisted safety latch held.
            self._loopback_owner = None

    @property
    def loopback_owner(self):
        return self._loopback_owner

    @property
    def loopback_pending(self):
        return bool(self._loopback_pending)

    def owns_pending_loopback(self, frame):
        return self.loopback_pending and self._loopback_owner is frame

    def persisted_loopback(self, frame):
        return self.loopback_pending and self._loopback_confirming is not frame

    @staticmethod
    def _physical_streams(audio):
        handles = getattr(audio, 'has_device_streams', None)
        return bool(audio.input_enabled or audio.output_enabled) if handles is None else handles

    def _claim(self, frame):
        if self.loopback_pending and self._loopback_owner is not frame:
            raise ValueError('Confirm that the interface loopback cable is disconnected before starting audio.')
        owner = self._route_owner
        if owner is not None and owner is not frame:
            if self._physical_streams(owner.audio) and owner.audio.running:
                raise ValueError('Another session is using the audio interface. Stop live input and playback in that session first.')
            if not owner.audio.running:
                # A worker failure can leave aborted PortAudio handles open.
                # Also disarm an interrupted tone so it cannot restart later.
                owner.audio.stop()
                owner.audio.set_generator(False)
                if generator := owner.pages.get('generate'):
                    generator.generating = False
                    generator._applied_generator_values = None
            self._route_owner = None
        self._route_owner = frame

    @contextmanager
    def route(self, frame, *, physical):
        if physical:
            self._claim(frame)
        try:
            yield
        finally:
            # Opening either device may fail. A software-only fallback may
            # still be running, but it does not reserve physical hardware.
            if self._route_owner is frame and not self._physical_streams(frame.audio):
                self._route_owner = None

    def stop_sounds(self):
        for frame in tuple(self._frames):
            frame.sounds.stop()

    def permits_sounds(self):
        return not self.loopback_pending and not any(frame._bench_capture is not None for frame in self._frames)

    def prepare_loopback(self, frame):
        self._claim(frame)
        self._loopback_pending = True
        self._loopback_owner = frame
        self.stop_sounds()
        frame.audio.set_interface_loopback_interlock(True)

    def begin_loopback_confirmation(self, frame):
        if not self.owns_pending_loopback(frame) or self._loopback_confirming is not None:
            return False
        # Let the owner save False before lifting the native gate. An unrelated
        # preferences write can never clear the pending-disconnect flag.
        self._loopback_confirming = frame
        return True

    def finish_loopback_confirmation(self, frame, *, saved):
        self._loopback_confirming = None
        if saved and self.owns_pending_loopback(frame):
            frame.audio.set_interface_loopback_interlock(False)
            self._loopback_pending = False
            self._loopback_owner = None

    def request_microphone(self, frame, request):
        self._microphone_waiters[frame] = None
        if self._microphone_requested:
            return
        self._microphone_requested = True
        try:
            request(self.microphone_answered)
        except (OSError, RuntimeError, ValueError):
            self._microphone_requested = False
            self._microphone_waiters.pop(frame, None)
            raise

    def microphone_answered(self, granted):
        self._microphone_requested = False
        waiters, self._microphone_waiters = self._microphone_waiters, {}
        for frame in waiters:
            if frame in self._frames:
                frame._microphone_answered(granted)
