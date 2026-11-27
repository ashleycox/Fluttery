"""Lossless physical input captured on a bounded background queue."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from itertools import groupby
from queue import Empty, Full, Queue
from struct import pack
from threading import Event, Thread

import numpy as np

MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_BLOCK_FRAMES = 384_000
MAX_AUDIO_CHANNELS = 64
ENCODINGS = {"pcm8": (1, 1), "pcm16": (1, 2), "pcm24": (1, 3), "pcm32": (1, 4),
             "f32le": (3, 4), "f64le": (3, 8)}


@dataclass(frozen=True, slots=True)
class AudioBlock:
    rate: int
    channels: int
    frames: int
    segment: int
    compressed: bytes
    encoding: str = "f32le"

    @property
    def size(self):
        return self.frames * self.channels * ENCODINGS[self.encoding][1]

    def decode(self):
        inflated = zlib.decompressobj()
        try:
            data = inflated.decompress(self.compressed, self.size + 1)
        except zlib.error as error:
            raise ValueError("The session contains a damaged input audio block.") from error
        if len(data) != self.size or not inflated.eof or inflated.unused_data:
            raise ValueError("The session contains an invalid input audio block.")
        return data


@dataclass(frozen=True, slots=True)
class CapturedInput:
    blocks: tuple[AudioBlock, ...]
    incomplete_reason: str = ""

    @property
    def seconds(self):
        return sum(block.frames / block.rate for block in self.blocks)

    @property
    def stored_bytes(self):
        return sum(len(block.compressed) for block in self.blocks)

    def segments(self):
        return [tuple(group) for _, group in groupby(
            self.blocks, key=lambda block: (block.segment, block.rate, block.channels, block.encoding),
        )]


def write_input_wav(stream, blocks):
    """Reproduce the input encoding and exact sample bytes in a new WAV."""
    if not blocks:
        raise ValueError("There is no captured input audio to export.")
    first = blocks[0]
    rate, channels = first.rate, first.channels
    if any((block.rate, block.channels, block.encoding) != (rate, channels, first.encoding) for block in blocks):
        raise ValueError("Export input with different sample formats as separate WAV files.")
    code, width = ENCODINGS[first.encoding]
    frames = sum(block.frames for block in blocks)
    size = frames * channels * width
    padding = size % 2
    if size + padding + 48 >= 2**32:
        raise ValueError("The captured audio exceeds the size of a standard WAV file.")
    # A fact chunk documents the number of samples in noninteger WAV audio.
    stream.write(pack("<4sI4s4sIHHIIHH4sII4sI", b"RIFF", size + padding + 48, b"WAVE", b"fmt ", 16,
                      code, channels, rate, rate * channels * width, channels * width, width * 8,
                      b"fact", 4, frames, b"data", size))
    for block in blocks:
        stream.write(block.decode())
    if padding:
        stream.write(b"\x00")


class SessionAudioCapture:
    """Compress input away from audio callbacks and keep it with the open session."""

    def __init__(self):
        self._blocks = []
        self._size = 0
        self._queue = Queue(256)
        self._closing = Event()
        self.incomplete_reason = ""
        self._segment = 0
        self._worker = Thread(target=self._record, name="Fluttery input capture", daemon=True)
        self._worker.start()

    def mark_gap(self, reason):
        self._segment += 1
        if not self.incomplete_reason:
            self.incomplete_reason = reason

    def append(self, frames, rate):
        if self._closing.is_set() or not len(frames):
            return
        rate = round(rate)
        block_size = max(1, min(rate, MAX_BLOCK_FRAMES))
        for offset in range(0, len(frames), block_size):
            if self._closing.is_set():
                break
            try:
                # The engine reuses its buffer after returning. Only a private
                # copy goes to the compressor, which never holds the engine lock.
                self._queue.put_nowait((rate, self._segment, frames[offset:offset + block_size].copy()))
            except Full:
                self.mark_gap("The input audio queue could not keep up.")
                break

    def finish(self):
        self._closing.set()
        self._worker.join()
        return CapturedInput(tuple(self._blocks), self.incomplete_reason) if self._blocks or self.incomplete_reason else None

    def _store_block(self, items, rate, segment):
        array = np.concatenate(items)
        data = array.astype("<f4", copy=False).tobytes()
        compressed = zlib.compress(data, level=3)
        if self._size + len(compressed) > MAX_AUDIO_BYTES:
            self.mark_gap("The input audio reached its 512 MiB compressed storage limit.")
            self._closing.set()
            return False
        self._blocks.append(AudioBlock(rate, array.shape[1], len(array), segment, compressed))
        self._size += len(compressed)
        return True

    def _record(self):
        items, count, previous = [], 0, None
        try:
            while not (self._closing.is_set() and self._queue.empty()):
                try:
                    rate, segment, data = self._queue.get(timeout=0.1)
                except Empty:
                    continue
                identity = rate, segment, data.shape[1]
                if items and (identity != previous or count + len(data) > min(rate, MAX_BLOCK_FRAMES)):
                    if not self._store_block(items, *previous[:2]):
                        return
                    items, count = [], 0
                items.append(data)
                count += len(data)
                previous = identity
            if items:
                self._store_block(items, *previous[:2])
        except (MemoryError, ValueError, zlib.error) as error:
            self.mark_gap(f"Input audio compression stopped. {error}")
        finally:
            self._closing.set()
            # On a storage limit or compression failure no worker remains
            # to consume these snapshots. Release them while the session is open.
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break
