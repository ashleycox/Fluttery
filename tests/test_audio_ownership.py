"""Keep the hardware, cable interlock and permission prompt process-wide."""

from types import SimpleNamespace

import pytest

from fluttery.audio_ownership import LOOPBACK_SETTING, AudioOwnership


class Audio:
    def __init__(self):
        self.running = False
        self.has_device_streams = False
        self.generator = False
        self.interlocked = False
        self.stopped = 0

    def stop(self):
        self.running = self.has_device_streams = False
        self.stopped += 1

    def set_generator(self, enabled):
        self.generator = enabled

    def set_interface_loopback_interlock(self, enabled):
        self.interlocked = enabled


class Frame:
    def __init__(self, pending=False):
        self.settings = {LOOPBACK_SETTING: pending}
        self._persistent_settings = self.settings.copy()
        self.audio = Audio()
        self.pages = {'generate': SimpleNamespace(generating=True, _applied_generator_values=('playing',))}
        self._bench_capture = None
        self.stopped_sounds = 0
        self.sounds = SimpleNamespace(stop=self._stop_sounds)
        self.permissions = []

    def _stop_sounds(self):
        self.stopped_sounds += 1

    def _microphone_answered(self, granted):
        self.permissions.append(granted)


def test_physical_route_is_exclusive_and_releases_a_faulted_transport():
    ownership = AudioOwnership()
    first, second = Frame(), Frame()
    ownership.register(first)
    ownership.register(second)
    with ownership.route(first, physical=True):
        first.audio.running = first.audio.has_device_streams = first.audio.generator = True
    with pytest.raises(ValueError, match='Another session'), ownership.route(second, physical=True):
        pytest.fail('A different session owns physical audio.')
    with ownership.route(second, physical=False):
        second.audio.running = True  # Internal loopback does not take the hardware.

    # The failed worker left open handles. Close and disarm it before the
    # replacement session can open streams.
    first.audio.running = False
    with ownership.route(second, physical=True):
        assert first.audio.stopped == 1 and not first.audio.has_device_streams
        assert not first.audio.generator and not first.pages['generate'].generating
        second.audio.has_device_streams = True


def test_loopback_blocks_other_sessions_even_when_the_owner_has_no_stream():
    ownership = AudioOwnership()
    owner, peer = Frame(), Frame(pending=True)
    ownership.register(owner)
    ownership.prepare_loopback(owner)
    ownership.register(peer)
    assert owner.audio.interlocked and not peer.audio.interlocked
    assert owner.stopped_sounds == 1
    assert not peer.settings[LOOPBACK_SETTING] and not peer._persistent_settings[LOOPBACK_SETTING]
    assert not ownership.permits_sounds() and ownership.persisted_loopback(peer)
    with pytest.raises(ValueError, match='loopback cable'), ownership.route(peer, physical=True):
        pytest.fail('The hardware gate is held even without a running stream.')
    assert not ownership.begin_loopback_confirmation(peer)
    assert ownership.begin_loopback_confirmation(owner)
    assert not ownership.persisted_loopback(owner) and ownership.persisted_loopback(peer)
    ownership.finish_loopback_confirmation(owner, saved=False)
    assert owner.audio.interlocked and ownership.persisted_loopback(owner)
    assert ownership.begin_loopback_confirmation(owner)
    ownership.finish_loopback_confirmation(owner, saved=True)
    assert not owner.audio.interlocked and not ownership.persisted_loopback(peer)
    assert ownership.permits_sounds()
    peer._bench_capture = object()
    assert not ownership.permits_sounds()


def test_microphone_permission_has_one_request_and_skips_closed_sessions():
    ownership = AudioOwnership()
    first, second = Frame(), Frame()
    ownership.register(first)
    ownership.register(second)
    requests = []
    ownership.request_microphone(first, requests.append)
    ownership.request_microphone(second, requests.append)
    assert len(requests) == 1
    ownership.unregister(first)
    requests.pop()(True)
    assert first.permissions == [] and second.permissions == [True]


def test_disconnect_recovery_survives_the_original_frame_and_a_failed_open():
    ownership = AudioOwnership()
    original, survivor, recovery = Frame(), Frame(), Frame()
    ownership.register(original)
    ownership.register(survivor)
    with pytest.raises(OSError, match='driver error'), ownership.route(original, physical=True):
        raise OSError('driver error')
    with ownership.route(survivor, physical=True):
        assert not original.audio.has_device_streams
    ownership.prepare_loopback(original)
    ownership.unregister(original)
    assert ownership.persisted_loopback(survivor)
    with pytest.raises(ValueError, match='loopback cable'), ownership.route(survivor, physical=True):
        pytest.fail('Closing its window cannot confirm physical disconnection.')
    ownership.register(recovery)
    assert ownership.owns_pending_loopback(recovery) and recovery.audio.interlocked
    assert recovery.settings[LOOPBACK_SETTING]
