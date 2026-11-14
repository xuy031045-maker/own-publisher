import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from browser_queue import QueueCancelled, queue_status, queued_browser


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)

    def test_waiting_task_can_be_cancelled_without_starting(self):
        entered, cancel = threading.Event(), threading.Event()
        outcome = []
        def waiting():
            try:
                with queued_browser('publish', cancel_requested=cancel.is_set,
                                    on_state=lambda state, detail: entered.set() if state == 'waiting' else None):
                    outcome.append('executed')
            except QueueCancelled:
                outcome.append('cancelled')
        with queued_browser('open'):
            worker = threading.Thread(target=waiting)
            worker.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual([x['state'] for x in queue_status()['tasks']], ['running', 'waiting'])
            cancel.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ['cancelled'])
        self.assertEqual(queue_status()['tasks'], [])

    def test_tasks_start_in_queue_order(self):
        order = []
        wait_a, wait_b = threading.Event(), threading.Event()
        def worker(name, waiting):
            with queued_browser('sync', on_state=lambda state, detail: waiting.set() if state == 'waiting' else None):
                order.append(name)
        with queued_browser('open'):
            first = threading.Thread(target=worker, args=('first', wait_a))
            second = threading.Thread(target=worker, args=('second', wait_b))
            first.start()
            self.assertTrue(wait_a.wait(5))
            second.start()
            self.assertTrue(wait_b.wait(5))
        first.join(5)
        second.join(5)
        self.assertEqual(order, ['first', 'second'])
        self.assertEqual(queue_status()['tasks'], [])

    def test_process_exit_releases_queue_without_replaying_operation(self):
        code = ('import time; from browser_queue import queued_browser\n'
                'with queued_browser("publish"):\n print("entered", flush=True)\n time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), 'entered')
        self.assertEqual(queue_status()['tasks'][0]['state'], 'running')
        child.kill()
        child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()
        with queued_browser('sync'):
            rows = queue_status()['tasks']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['kind'], 'sync')
        self.assertEqual(queue_status()['tasks'], [])


if __name__ == '__main__':
    unittest.main()
