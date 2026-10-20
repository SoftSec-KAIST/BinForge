"""Checks that variant slots are refilled as soon as a worker finishes."""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from binforge.utils import parallel_execute_func


class ParallelExecuteTests(unittest.TestCase):
    def test_completed_slot_starts_next_variant_before_slow_peer_finishes(self):
        release_slow = threading.Event()
        third_started = threading.Event()
        results = []

        def work(value):
            if value == 0:
                release_slow.wait(10)
            if value == 2:
                third_started.set()
            return value * 2

        def run():
            results.extend(parallel_execute_func(
                work, [0, 1, 2, 3], dynamic_arg_name="value", max_parallel_tasks=2))

        with patch("binforge.utils.ProcessPoolExecutor", ThreadPoolExecutor):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(third_started.wait(5),
                                "a completed slot did not start the next variant")
            finally:
                release_slow.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [0, 2, 4, 6])


if __name__ == "__main__":
    unittest.main()
