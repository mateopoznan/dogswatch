"""Run inside the container: python -m unittest discover -s tests"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

import app as pies  # noqa: E402


class StallWatchTest(unittest.TestCase):
    def silent_process(self):
        """Stands in for FFmpeg on a stream that stays open but sends nothing."""
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE)

    def test_silent_stream_is_killed(self):
        proc = self.silent_process()
        with mock.patch.object(pies, "STALL_SECONDS", 1):
            thread, stalled = pies.watch_for_stall(proc, [time.monotonic()], pies.source_version)
            data = pies.read_exact(proc.stdout, pies.frame_bytes)
            thread.join(5)
        self.assertEqual(data, b"")
        self.assertTrue(stalled.is_set())
        self.assertIsNotNone(proc.poll())

    def test_flowing_stream_is_left_alone(self):
        proc = self.silent_process()
        last_audio = [time.monotonic()]
        with mock.patch.object(pies, "STALL_SECONDS", 2):
            _, stalled = pies.watch_for_stall(proc, last_audio, pies.source_version)
            for _ in range(4):
                time.sleep(1)
                last_audio[0] = time.monotonic()
            self.assertFalse(stalled.is_set())
            self.assertIsNone(proc.poll())
        proc.kill()
        proc.wait()

    def test_source_change_stops_a_stalled_read(self):
        proc = self.silent_process()
        _, stalled = pies.watch_for_stall(proc, [time.monotonic()], pies.source_version - 1)
        self.assertEqual(pies.read_exact(proc.stdout, pies.frame_bytes), b"")
        self.assertTrue(stalled.is_set())


if __name__ == "__main__":
    unittest.main()
