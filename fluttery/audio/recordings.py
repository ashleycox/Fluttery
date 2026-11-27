"""Streaming PCM interchange for test recordings and demodulated speed."""

import json
import wave
import zlib
from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from math import ceil, isfinite
from pathlib import Path
from queue import Empty, Full, Queue
from struct import Struct, pack, unpack
from threading import Event, Thread

import numpy as np

from ..staging import atomic_output
from .processor import ANALYSIS_FRAMES, MeasurementProcessor
from .session_capture import MAX_AUDIO_BYTES, AudioBlock, CapturedInput

RIFF_HEADER = Struct('<4sI4s')
RIFF_CHUNK = Struct('<4sI')
WAV_FORMAT = Struct('<HHIIHH')
# Full scale in this file format denotes ±100% speed deviation. One percent
# therefore maps to 0.01 FS, irrespective of the tone frequency or sample rate.
SPEED_PCM_SCALE = 2 ** 31 / 100


@dataclass(frozen=True)
class WavInfo:
    rate: int
    channels: int
    bits: int
    floating: bool
    frames: int
    offset: int
    frame_bytes: int

    @property
    def duration(self):
        return self.frames / self.rate


def _inspect_wav(stream):
    header = stream.read(RIFF_HEADER.size)
    if len(header) != RIFF_HEADER.size:
        raise ValueError('This file does not contain a complete WAV header.')
    kind, size, name = RIFF_HEADER.unpack(header)
    if kind != b'RIFF' or name != b'WAVE':
        raise ValueError('Choose a little-endian PCM or IEEE float WAV file.')
    end = size + 8  # RIFF's declared size excludes its identifier and length.
    if end < RIFF_HEADER.size or end > stream.seek(0, 2):
        raise ValueError('The WAV file has an incomplete RIFF container.')
    stream.seek(RIFF_HEADER.size)
    metadata = data = None
    while stream.tell() < end:
        chunk = stream.read(min(RIFF_CHUNK.size, end - stream.tell()))
        if len(chunk) != RIFF_CHUNK.size:
            raise ValueError('A WAV chunk header is incomplete.')
        chunk_id, size = RIFF_CHUNK.unpack(chunk)
        offset = stream.tell()
        if offset + size > end:
            raise ValueError('The WAV file ends inside an audio chunk.')
        if chunk_id == b'fmt ' and metadata is None and size >= WAV_FORMAT.size:
            value = stream.read(min(size, 40))
            code, channels, rate, _, frame, bits = WAV_FORMAT.unpack_from(value)
            if code == 65534:
                # WAVE_FORMAT_EXTENSIBLE stores PCM/IEEE float in a GUID.
                if (len(value) < 40 or not 22 <= unpack('<H', value[16:18])[0] <= size - 18
                        or value[26:40] != bytes.fromhex('000000001000800000aa00389b71')):
                    raise ValueError('This WAV uses an unsupported sample encoding.')
                code, = unpack('<H', value[24:26])
            metadata = code, channels, rate, frame, bits
        elif chunk_id == b'data' and data is None:
            data = offset, size
        if metadata is not None and data is not None:
            break
        stream.seek(offset + size + (size & 1))
    if metadata is None or data is None:
        raise ValueError('The WAV file needs a format and an audio data chunk.')
    code, channels, rate, frame, bits = metadata
    if (code not in (1, 3) or bits not in ((32, 64) if code == 3 else (8, 16, 24, 32))
            or not 1 <= channels <= 64 or not 8_000 <= rate <= 384_000 or frame != channels * bits // 8):
        raise ValueError('Choose uncompressed PCM or IEEE float WAV at 8–384 kHz.')
    offset, size = data
    if size < frame or size % frame:
        raise ValueError('The WAV audio data is empty or ends inside a sample frame.')
    return WavInfo(rate, channels, bits, code == 3, size // frame, offset, frame)


def inspect_wav(path):
    with Path(path).open('rb') as stream:
        return _inspect_wav(stream)


def _decode(data, info):
    if info.floating:
        decoded = np.frombuffer(data, '<f4' if info.bits == 32 else '<f8')
        return np.nan_to_num(decoded, nan=0.0, posinf=0.0, neginf=0.0).reshape(-1, info.channels)
    if info.bits == 24:
        three = np.frombuffer(data, np.uint8).reshape(-1, 3).astype(np.int32)
        decoded = three[:, 0] | (three[:, 1] << 8) | (three[:, 2] << 16)
        decoded = (decoded ^ 0x800000) - 0x800000
    else:
        decoded = np.frombuffer(data, {8: 'u1', 16: '<i2', 32: '<i4'}[info.bits])
        if info.bits == 8:
            decoded = decoded.astype(np.int16) - 128
    return (decoded / (2 ** (info.bits - 1))).reshape(-1, info.channels)


def _plot_samples(blocks):
    """Retain extrema and lock gaps when compressing a long recording for display."""
    if len(blocks) <= 2:
        return blocks
    by_time = {0, len(blocks) - 1}
    loss = [_speed_loss_reason(row) is not None for row in blocks]
    locked = [(index, row) for index, row in enumerate(blocks) if not loss[index]]
    if locked:
        first = locked[0][1]
        peak_field = ('scrapePeak' if first['flutterBand'] == 'scrape' else
                      'aes6Peak' if first['weighting'] == 'aes6' else 'peak')
        for field, select in ((peak_field, max), ('rms', max), ('speedDrift', max), ('speedDrift', min)):
            valid = [(row[field], index) for index, row in locked if row.get(field) is not None]
            if valid:
                by_time.add(select(valid)[1])
    ordered = sorted(by_time)
    for left, right in pairwise(ordered):
        if not (loss[left] or loss[right]):
            # Every retained pair of valid readings must still be separated
            # by a gap if any intervening reading was invalid.
            unlocked = next((index for index in range(left + 1, right) if loss[index]), None)
            if unlocked is not None:
                by_time.add(unlocked)
    return [blocks[index] for index in sorted(by_time)]


def _speed_loss_reason(row):
    if row.get('clipped'):
        return 'Input clipped'
    if row.get('clockUnsupported'):
        return 'The corrected sample rate was outside the measurement range'
    if row.get('referenceAmbiguous'):
        return 'Reference tone was ambiguous'
    if row.get('referencePending'):
        return 'Reference change in progress'
    if not row['locked']:
        return 'Reference tone unlocked'
    if row.get('invalidFrames'):
        return 'Part of the input block could not be demodulated'
    return None


def analyse_wav(path, settings, *, channel=0, start=0.0, duration=None, cancelled=None, progress=None, retain_input=False):
    """Analyse raw PCM through the same processor as live audio, with bounded memory."""
    if not isfinite(start) or (duration is not None and not isfinite(duration)):
        raise ValueError('Choose finite start and duration values.')
    with Path(path).open('rb') as stream:
        info = _inspect_wav(stream)
        if not 0 <= channel < info.channels:
            raise ValueError('Choose a channel that is present in the recording.')
        start_frame = round(min(info.duration, max(0, start)) * info.rate)
        frames = info.frames - start_frame
        if duration is not None:
            frames = round(min(frames / info.rate, duration) * info.rate)
        if frames <= 0:
            raise ValueError('Select a time range that contains audio.')
        stream.seek(info.offset + start_frame * info.frame_bytes)
        processor = MeasurementProcessor({**settings, 'channels': 1}, sample_rate=info.rate)
        if processor.clock_unsupported:
            raise ValueError('The saved clock correction puts the WAV outside the supported measurement range.')
        rate = processor.measurement_rate
        complete = settings.get('keepCompleteHistory')
        interval = 1 if complete else max(1, ceil(frames / (120 * ANALYSIS_FRAMES)))
        history, pending = [], []
        locked_blocks = 0
        valid_seconds = 0.0
        peak = None
        last_locked = None
        diagnostics = {}
        latest_spectrum = {}
        spectra = deque(maxlen=None if complete else 1200)
        spectrum_time = -1.0
        audio_chunks, audio_buffer, audio_size = [], bytearray(), 0
        audio_error = ''
        encoding = f'f{info.bits}le' if info.floating else f'pcm{info.bits}'
        losses = [] if settings.get('keepSignalLossDetails') else None
        current_loss = None

        def retain(data):
            nonlocal audio_size, audio_error
            if not retain_input or audio_error:
                return
            audio_buffer.extend(data)
            if len(audio_buffer) < info.rate * info.frame_bytes // 2 and position < frames:
                return
            # Capture the actual WAV sample bytes, preserving even 32-bit
            # integers and 64-bit floats exactly. Analysis uses its own decode.
            compressed = zlib.compress(audio_buffer, level=3)
            if audio_size + len(compressed) > MAX_AUDIO_BYTES:
                audio_error = 'The input audio reached its 512 MiB compressed storage limit.'
            else:
                audio_chunks.append(AudioBlock(info.rate, info.channels, len(audio_buffer) // info.frame_bytes,
                                               0, compressed, encoding))
                audio_size += len(compressed)
            audio_buffer.clear()

        def observe(row, count):
            nonlocal locked_blocks, valid_seconds, peak, last_locked, latest_spectrum, spectrum_time
            nonlocal current_loss
            timestamp = row['timestamp']
            reason = _speed_loss_reason(row)
            if reason:
                if losses is not None:
                    if current_loss is None or current_loss['reason'] != reason:
                        current_loss = {'start': max(0, timestamp - count / rate),
                                        'end': timestamp, 'reason': reason}
                        losses.append(current_loss)
                    current_loss['end'] = timestamp
                return
            current_loss = None
            locked_blocks += 1
            valid_seconds += count / rate
            last_locked = row
            for field in ('aes6Peak', 'rmsOneSecond', 'driftRms', 'scrapeRms', 'scrapePeak',
                          'flutterPeriodHz', 'flutterPeriodSeconds', 'rollingRmsMax', 'rollingPeakMax'):
                if row.get(field) is not None:
                    diagnostics[field] = row[field]
            field = ('scrapePeak' if row['flutterBand'] == 'scrape' else 'aes6Peak' if row['weighting'] == 'aes6' else 'peak')
            if (reading := row.get(field)) is not None:
                peak = max(peak or 0.0, reading)
            if timestamp - spectrum_time >= 1 or position == frames:
                spectrum = processor.snapshot()['flutter_spectrum']
                if len(spectrum.get('db', ())) >= 2:
                    latest_spectrum = spectrum
                    spectra.append({**spectrum, 'timestamp': timestamp})
                    spectrum_time = timestamp

        position = 0
        block = 0
        while position < frames:
            if cancelled is not None and cancelled():
                return None
            count = min(ANALYSIS_FRAMES, frames - position)
            data = stream.read(count * info.frame_bytes)
            if len(data) != count * info.frame_bytes:
                raise OSError('The WAV recording changed or was truncated during analysis.')
            processor.feed(_decode(data, info)[:, channel])
            position += count
            retain(data)
            for row in processor.drain_measurements():
                observe(row, count)
                pending.append(row)
                if len(pending) >= interval:
                    history.extend(_plot_samples(pending))
                    pending.clear()
            block += 1
            if progress is not None and (block % 16 == 0 or position == frames):
                progress(position / frames)
        if cancelled is not None and cancelled():
            return None
        processor.wow_flutter_summary()
        for row in processor.drain_measurements():
            observe(row, frames % ANALYSIS_FRAMES)
            pending.append(row)
        history.extend(_plot_samples(pending))
        if not locked_blocks:
            raise ValueError('No stable reference tone was found on this channel in the selected interval.')
        result = {'metrics': {**last_locked, **diagnostics}, 'history': history, 'rate': info.rate,
                  'channel': channel + 1, 'start': start_frame / rate, 'duration': frames / rate,
                  'validSeconds': valid_seconds, 'maximumPeak': peak, 'spectrum': latest_spectrum,
                  'spectra': list(spectra)}
        if retain_input:
            result['inputAudio'] = CapturedInput(tuple(audio_chunks), audio_error)
        if losses is not None:
            result['signalLoss'] = losses
        return result


class _DiscardSpeedFile(Exception):
    pass


class RawSpeedCapture:
    """Drain a bounded speed-data queue to an atomic PCM WAV on a file worker."""

    def __init__(self, target, rate, tape_end=None):
        self.target = Path(target)
        self.rate = round(rate)
        self.frames = 0
        self.error = ''
        self.saved = False
        self.ended_at_tape = False
        self._tape_end = TapeEndDetector(*tape_end) if tape_end is not None else None
        self._cancel = False
        self._queue = Queue(64)
        self._stop = Event()
        self.done = Event()
        self._worker = Thread(target=self._write, name='Fluttery speed waveform')
        self._worker.start()

    def append(self, values, valid):
        if self._stop.is_set() or not len(values):
            return
        frames = np.asarray(values).reshape(-1)
        usable = np.isfinite(frames)
        if valid is not None:
            usable &= np.asarray(valid, dtype=bool).reshape(-1)
        try:
            self._queue.put_nowait((np.where(usable, frames, 0.0), usable))
        except Full:
            self.finish(error='The disk could not keep up with the speed recording.')

    def finish(self, *, cancel=False, error=''):
        self._cancel |= cancel
        self.error = self.error or error
        self._stop.set()

    def observe_end(self, metrics):
        if self._tape_end is not None and self._tape_end.observe(
            metrics['timestamp'], metrics['locked'], metrics['inputAcLevel']
        ):
            self.ended_at_tape = True
            self.finish()
        return self.ended_at_tape

    @property
    def seconds(self):
        return self.frames / self.rate

    def _write(self):
        gaps = []
        try:
            with atomic_output(self.target) as file:
                with wave.open(file, 'wb') as output:
                    output.setparams((1, 4, self.rate, 0, 'NONE', 'not compressed'))
                    while not (self._cancel or self.error):
                        try:
                            frames, usable = self._queue.get(timeout=0.1)
                        except Empty:
                            if self._stop.is_set():
                                break
                            continue
                        limit = 2 ** 31
                        scaled = np.rint(frames.astype(np.float64) * SPEED_PCM_SCALE)
                        pcm = np.clip(scaled, -limit, limit - 1).astype('<i4')
                        if 36 + (self.frames + len(frames)) * 4 >= 2 ** 32:
                            raise ValueError('This recording exceeds the 4 GB limit of a standard WAV file.')
                        output.writeframesraw(pcm.tobytes())
                        for first, last in np.flatnonzero(np.diff(np.r_[False, ~usable, False])).reshape(-1, 2):
                            start, end = self.frames + int(first), self.frames + int(last)
                            if gaps and gaps[-1][1] == start:
                                gaps[-1][1] = end
                            else:
                                gaps.append([start, end])
                        self.frames += len(frames)
                if self._cancel or self.error:
                    raise _DiscardSpeedFile
                if not self.frames:
                    raise ValueError('No speed samples were captured.')
                _write_speed_gaps(file, gaps)
            self.saved = True
        except _DiscardSpeedFile:
            pass
        except (OSError, ValueError, OverflowError, wave.Error) as error:
            self.error = str(error)
        finally:
            self.done.set()


def _write_speed_gaps(file, gaps):
    if not gaps:
        return
    # Ordinary audio players still read the PCM. Fluttery-aware readers can
    # distinguish samples without a reference from a genuine zero deviation.
    data = json.dumps({'version': 1, 'invalidFrames': gaps}, separators=(',', ':')).encode('ascii')
    size = file.seek(0, 2) + RIFF_CHUNK.size + len(data) + len(data) % 2
    if size - 8 >= 2**32:
        raise ValueError('The speed recording and its gap information exceed the WAV file size limit.')
    file.write(RIFF_CHUNK.pack(b'flty', len(data)))
    file.write(data)
    if len(data) % 2:
        file.write(b'\x00')
    file.seek(4)
    file.write(pack('<I', size - 8))


class TapeEndDetector:
    """Stop after sustained signal loss, but only after a valid test tone."""

    def __init__(self, silence_seconds, threshold_db, armed_seconds=2):
        self.silence_seconds = silence_seconds
        self.threshold_db = threshold_db
        self.armed_seconds = armed_seconds
        self.valid_since = None
        self.silent_since = None
        self.armed = False

    def observe(self, timestamp, locked, level):
        if locked:
            if self.valid_since is None:
                self.valid_since = timestamp
            self.armed |= timestamp - self.valid_since >= self.armed_seconds
        else:
            self.valid_since = None
        if locked or level > self.threshold_db or not self.armed:
            self.silent_since = None
            return False
        if self.silent_since is None:
            self.silent_since = timestamp
        return timestamp - self.silent_since >= self.silence_seconds
