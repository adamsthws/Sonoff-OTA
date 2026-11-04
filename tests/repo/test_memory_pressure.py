import unittest

from tools.repo import memory_pressure as mp


class MemoryPressureTests(unittest.TestCase):
    def test_memory_short_or_held_up_on_it_is_starved(self):
        self.assertTrue(mp.memory_note(mp.LOW_MEMORY_KB - 1, 0, 0, 100)["starved"])
        ample = mp.LOW_MEMORY_KB + 1
        self.assertFalse(mp.memory_note(ample, 0, 14 * 1_000_000, 100)["starved"])
        held_up = mp.memory_note(ample, 0, 15 * 1_000_000, 100)
        self.assertTrue(held_up["starved"])
        self.assertEqual(held_up["stalled_percent"], 15.0)

    def test_without_the_kernels_stall_figures(self):
        note = mp.memory_note(mp.LOW_MEMORY_KB + 1, None, None, 100)
        self.assertIsNone(note["stalled_percent"])
        self.assertFalse(note["starved"])

    def test_it_reads_this_computer(self):
        available, stalled = mp.memory_now()
        self.assertGreater(available, 0)
        self.assertTrue(stalled is None or stalled >= 0)

    def test_the_watch_notes_what_it_saw(self):
        watch = mp.MemoryWatch()
        watch.start()
        watch.lowest = mp.LOW_MEMORY_KB - 1
        note = watch.note()
        watch.join(6)
        self.assertFalse(watch.is_alive())
        self.assertTrue(note["starved"])


if __name__ == "__main__":
    unittest.main()
