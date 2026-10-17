"""Exercise the scheduler with real short-lived subprocesses, without a GPU."""
import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

# Load a private copy so other tests' module stubs cannot affect this suite.
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('queue_tasks_test', ROOT / 'mikazuki/tasks.py')
queue = importlib.util.module_from_spec(spec)
try:
    import psutil
except ImportError:
    # Only process-tree termination needs psutil; patched explicitly below.
    with patch.dict(sys.modules, {'psutil': types.ModuleType('psutil')}):
        spec.loader.exec_module(queue)
else:
    spec.loader.exec_module(queue)


def eventually(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Timed out waiting for scheduler')


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.manager = queue.TaskManager()
        self.gate = threading.Event()
        self.started = []
        self.original_execute = queue.Task.execute
        def execute(task):
            self.started.append(task.task_id)
            self.original_execute(task)
        self.spy = patch.object(queue.Task, 'execute', execute)
        self.spy.start()

    def tearDown(self):
        self.gate.set()
        for task in list(self.manager.tasks.values()):
            if task.process is not None and task.process.poll() is None:
                task.process.kill()
        eventually(lambda: not self.manager._active)
        self.spy.stop()

    def submit(self, code='pass'):
        return self.manager.create_task([sys.executable, '-c', code], os.environ)

    def hold_first(self, code="pass"):
        original_wait = queue.Task.wait
        def wait(task):
            self.gate.wait(5)
            original_wait(task)
        self.wait_patch = patch.object(queue.Task, 'wait', wait)
        self.wait_patch.start()
        self.addCleanup(self.wait_patch.stop)
        task = self.submit(code)
        eventually(lambda: task.status == queue.TaskStatus.RUNNING)
        return task

    def test_fifo_cancel_and_positions(self):
        first = self.hold_first()
        cancelled = self.submit()
        third = self.submit()
        self.assertEqual([t['queue_position'] for t in self.manager.dump()], [None, 1, 2])
        self.assertTrue(self.manager.terminate_task(cancelled.task_id))
        self.assertEqual(self.manager.dump()[-1]['queue_position'], 1)
        self.gate.set()
        eventually(lambda: not self.manager._active)
        self.assertEqual(self.started, [first.task_id, third.task_id])
        self.assertEqual(cancelled.status, queue.TaskStatus.TERMINATED)
        self.assertTrue(queue.hub.is_done(cancelled.task_id))

    def test_nonzero_exit_continues(self):
        first = self.manager.create_task([sys.executable, '-c', 'raise SystemExit(3)'], os.environ)
        second = self.submit()
        eventually(lambda: not self.manager._active)
        self.assertEqual(first.status, queue.TaskStatus.FAILED)
        self.assertEqual(first.returncode, 3)
        self.assertEqual(second.status, queue.TaskStatus.FINISHED)

    def test_launch_failure_continues(self):
        first = self.manager.create_task(['this-executable-does-not-exist-queue-test'], os.environ)
        second = self.submit()
        eventually(lambda: not self.manager._active)
        self.assertEqual(first.status, queue.TaskStatus.FAILED)
        self.assertEqual(second.status, queue.TaskStatus.FINISHED)

    def test_parallel_submissions_are_serialized(self):
        first = self.hold_first()
        threads = [threading.Thread(target=self.submit) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.started, [first.task_id])
        expected = list(self.manager.tasks)
        self.gate.set()
        eventually(lambda: not self.manager._active)
        self.assertEqual(self.started, expected)

    def test_running_cancel_keeps_slot_until_exit(self):
        first = self.hold_first("import time; time.sleep(60)")
        second = self.submit()
        with patch.object(queue, 'kill_proc_tree', side_effect=lambda *args: first.process.kill()) as kill:
            self.manager.terminate_task(first.task_id)
            kill.assert_called_once_with(first.process.pid, True)
        self.assertEqual(self.started, [first.task_id])
        self.gate.set()
        eventually(lambda: not self.manager._active)
        self.assertEqual(first.status, queue.TaskStatus.TERMINATED)
        self.assertEqual(second.status, queue.TaskStatus.FINISHED)

    def test_worker_start_failure_does_not_reserve_slot(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('no thread')):
            first = self.submit()
        self.assertEqual(first.status, queue.TaskStatus.FAILED)
        self.assertFalse(self.manager._active)
        second = self.submit()
        eventually(lambda: not self.manager._active)
        self.assertEqual(second.status, queue.TaskStatus.FINISHED)

    def test_stdout_thread_failure_reaps_process_before_next(self):
        original_start = threading.Thread.start
        def start(thread):
            if thread._target == queue.Task._stdout_pump or getattr(thread._target, '__name__', '') == '_stdout_pump':
                raise RuntimeError('no stdout thread')
            return original_start(thread)
        import signal
        with patch.object(threading.Thread, 'start', start), patch.object(
                queue, 'kill_proc_tree', side_effect=lambda pid, _: os.kill(pid, signal.SIGTERM)):
            first = self.submit('import time; time.sleep(60)')
            eventually(lambda: not self.manager._active)
        self.assertIsNotNone(first.process.poll())
        self.assertEqual(first.status, queue.TaskStatus.FAILED)
        second = self.submit()
        eventually(lambda: not self.manager._active)
        self.assertEqual(second.status, queue.TaskStatus.FINISHED)

    def test_stdout_eof_does_not_signal_training_done(self):
        from unittest.mock import MagicMock
        task = queue.Task('eof-test', [], {})
        queue.hub.start_task(task.task_id)
        task.process = MagicMock()
        task.process.stdout.readline.return_value = ''
        task._stdout_pump()
        self.assertFalse(queue.hub.is_done(task.task_id))

    def test_exited_child_does_not_abort_tree_cleanup(self):
        from unittest.mock import MagicMock
        class Gone(Exception):
            pass
        parent, child1, child2 = MagicMock(), MagicMock(), MagicMock()
        parent.children.return_value = [child1, child2]
        child1.kill.side_effect = Gone()
        fake_psutil = types.SimpleNamespace(Process=lambda pid: parent,
            NoSuchProcess=Gone, wait_procs=MagicMock(return_value=([], [])))
        with patch.object(queue, 'psutil', fake_psutil):
            queue.kill_proc_tree(123, True)
        child2.kill.assert_called_once()
        parent.kill.assert_called_once()
        fake_psutil.wait_procs.assert_called_once_with([child1, child2, parent], timeout=5)

    def test_unknown_cancel(self):
        self.assertFalse(self.manager.terminate_task('missing'))


class ShutdownTests(unittest.TestCase):
    """No test is allowed to invoke the real host shutdown function."""
    def setUp(self):
        from unittest.mock import Mock
        self.timers = []
        timers = self.timers
        class FakeTimer:
            def __init__(self, delay, callback, args):
                self.delay, self.callback, self.args = delay, callback, args
                self.cancelled = False
                timers.append(self)
            def start(self):
                pass
            def cancel(self):
                self.cancelled = True
            def fire(self):
                self.callback(*self.args)
        self.timer_patch = patch.object(queue.threading, 'Timer', FakeTimer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.platform_patch = patch.object(queue.sys, 'platform', 'win32')
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        self.manager = queue.TaskManager()
        self.manager._shutdown_action = Mock()
        self.addCleanup(self.manager._cancel_shutdown_locked)

    def seed_running(self):
        task = queue.Task('running', [], {})
        task.status = queue.TaskStatus.RUNNING
        self.manager.tasks[task.task_id] = task
        self.manager._active.add(task.task_id)
        return task

    def finish(self, task, status=queue.TaskStatus.FINISHED):
        with self.manager._lock:
            task.status = status
            self.manager._active.discard(task.task_id)
            self.manager._maybe_shutdown_locked()

    def arm_and_finish(self):
        task = self.seed_running()
        self.manager.set_shutdown(True)
        self.finish(task)
        return self.timers[-1]

    def test_default_off_and_enabling_idle_does_not_shutdown(self):
        self.assertFalse(self.manager.shutdown_status()['enabled'])
        self.manager.set_shutdown(True)
        self.assertEqual(self.timers, [])
        self.manager._shutdown_action.assert_not_called()

    def test_old_completed_tasks_do_not_start_countdown(self):
        task = self.seed_running()
        self.finish(task)
        self.manager.set_shutdown(True)
        self.assertEqual(self.timers, [])

    def test_countdown_starts_only_after_active_slot_released(self):
        task = self.seed_running()
        self.manager.set_shutdown(True)
        task.status = queue.TaskStatus.FINISHED
        self.manager._maybe_shutdown_locked()
        self.assertFalse(self.timers)
        self.finish(task)
        self.assertEqual(self.timers[0].delay, 60)
        self.assertGreater(self.manager.shutdown_status()['remaining_seconds'], 0)
        self.manager._shutdown_action.assert_not_called()
        self.timers[0].fire()
        self.manager._shutdown_action.assert_called_once()
        self.assertFalse(self.manager.shutdown_status()['enabled'])
        self.assertTrue(self.manager.shutdown_status()['triggered'])
        self.timers[0].fire()
        self.manager._shutdown_action.assert_called_once()

    def test_disable_cancels_even_a_callback_already_queued(self):
        timer = self.arm_and_finish()
        self.manager.set_shutdown(False)
        timer.fire()
        self.manager._shutdown_action.assert_not_called()
        self.assertTrue(timer.cancelled)
        self.assertIsNone(self.manager.shutdown_status()['remaining_seconds'])

    def test_new_submission_cancels_old_timer_then_rearms(self):
        timer = self.arm_and_finish()
        with patch.object(self.manager, '_schedule_locked'):
            new = self.manager.create_task(['unused'], {})
        timer.fire()
        self.manager._shutdown_action.assert_not_called()
        self.assertTrue(timer.cancelled)
        self.assertIsNone(self.manager.shutdown_status()['remaining_seconds'])
        self.finish(new)
        self.assertEqual(len(self.timers), 2)
        self.timers[-1].fire()
        self.manager._shutdown_action.assert_called_once()

    def test_cancellation_wins_over_callback_waiting_for_lock(self):
        timer = self.arm_and_finish()
        entered = threading.Event()
        def fire():
            entered.set()
            timer.fire()
        with self.manager._lock:
            thread = threading.Thread(target=fire)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.manager.set_shutdown(False)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.manager._shutdown_action.assert_not_called()

    def test_slow_submission_protects_against_old_callback(self):
        timer = self.arm_and_finish()
        submission = threading.Event()
        with self.manager._lock:
            thread = threading.Thread(target=timer.fire)
            thread.start()
            self.manager.begin_submission(submission)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.manager.shutdown_status()['remaining_seconds'])
        self.assertTrue(self.manager._queue_busy_locked())
        self.manager._shutdown_action.assert_not_called()
        # Validation failed, so there is no new task, but a full countdown resumes.
        self.manager.end_submission(submission)
        self.assertEqual(len(self.timers), 2)
        self.assertGreaterEqual(self.manager.shutdown_status()['remaining_seconds'], 59)
        timer.fire()
        self.manager._shutdown_action.assert_not_called()

    def test_new_job_cannot_start_after_shutdown_was_dispatched(self):
        timer = self.arm_and_finish()
        timer.fire()
        with self.assertRaises(RuntimeError):
            self.manager.create_task(['unused'], {})
        with self.assertRaises(RuntimeError):
            self.manager.begin_submission(threading.Event())
        with self.assertRaises(ValueError):
            self.manager.set_shutdown(False)

    def test_late_cancel_reports_failure_when_dispatch_won_race(self):
        timer = self.arm_and_finish()
        dispatched, release = threading.Event(), threading.Event()
        errors = []
        def action():
            dispatched.set()
            release.wait(3)
        self.manager._shutdown_action.side_effect = action
        callback = threading.Thread(target=timer.fire)
        def cancel():
            try:
                self.manager.set_shutdown(False)
            except ValueError as exc:
                errors.append(str(exc))
        callback.start()
        self.assertTrue(dispatched.wait(2))
        cancellation = threading.Thread(target=cancel)
        cancellation.start()
        release.set()
        callback.join(2)
        cancellation.join(2)
        self.assertFalse(callback.is_alive())
        self.assertFalse(cancellation.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('无法取消', errors[0])
        self.manager._shutdown_action.assert_called_once()

    def test_repeated_cancel_and_submit_races_never_shutdown_after_success(self):
        from unittest.mock import Mock
        for operation in ('cancel', 'submit'):
            for attempt in range(100):
                with self.subTest(operation=operation, attempt=attempt):
                    manager = queue.TaskManager()
                    manager._shutdown_action = Mock()
                    running = queue.Task('running', [], {})
                    running.status = queue.TaskStatus.RUNNING
                    manager.tasks['running'] = running
                    manager._active.add('running')
                    manager.set_shutdown(True)
                    with manager._lock:
                        running.status = queue.TaskStatus.FINISHED
                        manager._active.clear()
                        manager._maybe_shutdown_locked()
                    timer = self.timers[-1]
                    barrier = threading.Barrier(3)
                    submission = threading.Event()
                    result = {}
                    def callback():
                        barrier.wait(2)
                        timer.fire()
                    def request():
                        barrier.wait(2)
                        try:
                            if operation == 'cancel':
                                manager.set_shutdown(False)
                            else:
                                manager.begin_submission(submission)
                            result['accepted'] = True
                        except (RuntimeError, ValueError):
                            result['accepted'] = False
                    callback_thread = threading.Thread(target=callback)
                    request_thread = threading.Thread(target=request)
                    callback_thread.start()
                    request_thread.start()
                    barrier.wait(2)
                    callback_thread.join(2)
                    request_thread.join(2)
                    self.assertFalse(callback_thread.is_alive())
                    self.assertFalse(request_thread.is_alive())
                    self.assertIn('accepted', result)
                    if result['accepted']:
                        manager._shutdown_action.assert_not_called()
                        timer.fire()  # Even a second delivery of the stale callback is inert.
                        manager._shutdown_action.assert_not_called()
                        manager.set_shutdown(False)
                        manager.end_submission(submission)
                    else:
                        manager._shutdown_action.assert_called_once()
                    manager._cancel_shutdown_locked()

    def test_reenable_does_not_revive_old_timer(self):
        timer = self.arm_and_finish()
        self.manager.set_shutdown(False)
        self.manager.set_shutdown(True)
        timer.fire()
        self.manager._shutdown_action.assert_not_called()

    def test_disable_during_submission_stays_disabled_after_release(self):
        timer = self.arm_and_finish()
        submission = threading.Event()
        self.manager.begin_submission(submission)
        self.manager.set_shutdown(False)
        self.manager.end_submission(submission)
        timer.fire()
        self.manager._shutdown_action.assert_not_called()
        self.assertFalse(self.manager.shutdown_status()['enabled'])
        self.assertEqual(len(self.timers), 1)

    def test_multiple_submissions_must_all_release_before_countdown(self):
        self.arm_and_finish()
        first, second = threading.Event(), threading.Event()
        self.manager.begin_submission(first)
        self.manager.begin_submission(second)
        self.manager.end_submission(first)
        self.assertIsNone(self.manager.shutdown_status()['remaining_seconds'])
        self.manager.end_submission(second)
        self.assertEqual(len(self.timers), 2)

    def test_cancelled_http_request_cannot_register_late(self):
        self.arm_and_finish()
        submission = threading.Event()
        # end_submission can run before a delayed to_thread(begin_submission).
        self.manager.end_submission(submission)
        self.manager.begin_submission(submission)
        self.assertFalse(self.manager._pending_submissions)

    def test_timer_constructor_failure_is_reported(self):
        task = self.seed_running()
        self.manager.set_shutdown(True)
        with patch.object(queue.threading, 'Timer', side_effect=RuntimeError('timer unavailable')):
            self.finish(task)
        self.assertFalse(self.manager.shutdown_status()['enabled'])
        self.assertIn('timer unavailable', self.manager.shutdown_status()['error'])

    def test_real_jobs_trigger_only_after_last_job_exits(self):
        self.manager.set_shutdown(True)
        with patch.object(self.manager, '_schedule_locked'):
            first = self.manager.create_task([sys.executable, '-c', 'pass'], os.environ)
            second = self.manager.create_task([sys.executable, '-c', 'pass'], os.environ)
        with self.manager._lock:
            self.manager._schedule_locked()
        eventually(lambda: not self.manager._active)
        self.assertEqual(first.status, queue.TaskStatus.FINISHED)
        self.assertEqual(second.status, queue.TaskStatus.FINISHED)
        self.assertEqual(len(self.timers), 1)
        self.manager._shutdown_action.assert_not_called()
        self.timers[0].fire()
        self.manager._shutdown_action.assert_called_once()

    def test_failed_or_cancelled_tasks_also_finish_queue(self):
        for status in (queue.TaskStatus.FAILED, queue.TaskStatus.TERMINATED):
            with self.subTest(status=status):
                self.manager.set_shutdown(False)
                task = self.seed_running()
                self.manager.set_shutdown(True)
                self.finish(task, status)
                self.assertIsNotNone(self.manager.shutdown_status()['remaining_seconds'])

    def test_queued_task_prevents_shutdown(self):
        timer = self.arm_and_finish()
        task = queue.Task('queued', [], {})
        task.status = queue.TaskStatus.QUEUED
        self.manager.tasks[task.task_id] = task
        timer.fire()
        self.manager._shutdown_action.assert_not_called()

    def test_failure_is_reported_and_not_retried(self):
        self.manager._shutdown_action.side_effect = PermissionError('not permitted')
        timer = self.arm_and_finish()
        timer.fire()
        state = self.manager.shutdown_status()
        self.assertIn('not permitted', state['error'])
        self.assertFalse(state['enabled'])
        self.assertFalse(state['triggered'])
        self.manager._maybe_shutdown_locked()
        self.assertEqual(len(self.timers), 1)
        timer.fire()
        self.manager._shutdown_action.assert_called_once()

    def test_repeated_enable_preserves_countdown(self):
        timer = self.arm_and_finish()
        self.manager.set_shutdown(True)
        self.assertIs(self.manager._shutdown_timer, timer)

    def test_unsupported_platform_rejects_enable(self):
        with patch.object(queue.sys, 'platform', 'unsupported'):
            with self.assertRaises(ValueError):
                self.manager.set_shutdown(True)
            self.assertFalse(self.manager.shutdown_status()['supported'])

    def test_linux_shutdown_wrapper_without_shebang_uses_bash(self):
        import errno
        from unittest.mock import mock_open
        error = OSError(errno.ENOEXEC, 'Exec format error', 'shutdown')
        with patch.object(queue.sys, 'platform', 'linux'), \
                patch.object(queue.subprocess, 'run', side_effect=[error, None]) as run, \
                patch.object(queue.shutil, 'which', side_effect=['/cloud/shutdown', '/bin/bash']), \
                patch('builtins.open', mock_open(read_data=b'echo fake-power-off\n')):
            queue.shutdown_host()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ['/bin/bash', '/cloud/shutdown', '-h', 'now'])
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_permission_denied_does_not_retry_in_shell(self):
        import errno
        error = PermissionError(errno.EACCES, 'Permission denied')
        with patch.object(queue.sys, 'platform', 'linux'), \
                patch.object(queue.subprocess, 'run', side_effect=error) as run:
            with self.assertRaises(PermissionError):
                queue.shutdown_host()
        run.assert_called_once()

    def test_invalid_binary_does_not_retry_in_shell(self):
        import errno
        from unittest.mock import mock_open
        error = OSError(errno.ENOEXEC, 'Exec format error')
        with patch.object(queue.sys, 'platform', 'linux'), \
                patch.object(queue.subprocess, 'run', side_effect=error) as run, \
                patch.object(queue.shutil, 'which', side_effect=['/cloud/shutdown', '/bin/bash']), \
                patch('builtins.open', mock_open(read_data=b'\x7fELF\0binary')):
            with self.assertRaises(OSError):
                queue.shutdown_host()
        run.assert_called_once()

    def test_wrapper_failure_remains_visible_to_shutdown_manager(self):
        import errno
        from unittest.mock import mock_open
        error = OSError(errno.ENOEXEC, 'Exec format error')
        failed = queue.subprocess.CalledProcessError(1, ['bash'], stderr='wrapper failed')
        with patch.object(queue.sys, 'platform', 'linux'), \
                patch.object(queue.subprocess, 'run', side_effect=[error, failed]), \
                patch.object(queue.shutil, 'which', side_effect=['/cloud/shutdown', '/bin/bash']), \
                patch('builtins.open', mock_open(read_data=b'false\n')):
            self.manager._shutdown_action = queue.shutdown_host
            timer = self.arm_and_finish()
            timer.fire()
        self.assertIn('wrapper failed', self.manager.shutdown_status()['error'])
        self.assertFalse(self.manager.shutdown_status()['triggered'])

    def test_shutdown_command_is_mocked_and_does_not_force_close(self):
        for platform in ('win32', 'linux'):
            with self.subTest(platform=platform), patch.object(queue.sys, 'platform', platform), \
                    patch.object(queue.subprocess, 'run') as run, \
                    patch.object(queue.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
                queue.shutdown_host()
                command = run.call_args.args[0]
                if platform == 'win32':
                    self.assertEqual(command[1:], ['/s', '/t', '0'])
                    self.assertNotIn('/f', command)
                else:
                    self.assertEqual(command, ['shutdown', '-h', 'now'])
                self.assertTrue(run.call_args.kwargs['check'])


class SubmissionRequestTests(unittest.IsolatedAsyncioTestCase):
    """Run the actual handler body without importing optional GUI dependencies."""
    async def asyncSetUp(self):
        import ast
        import asyncio
        import json
        import uuid
        from datetime import datetime
        from unittest.mock import Mock
        module = ast.parse((ROOT / 'mikazuki/app/api.py').read_text(encoding='utf-8'))
        handler = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == 'create_toml_file')
        handler.decorator_list = []
        for argument in handler.args.args:
            argument.annotation = None
        handler.returns = None
        self.manager = queue.TaskManager()
        self.manager._shutdown_action = Mock()
        self.body_entered = asyncio.Event()
        self.release_body = asyncio.Event()
        async def body():
            self.body_entered.set()
            await self.release_body.wait()
            return b'{"train_data_dir":"missing","pretrained_model_name_or_path":"unused"}'
        self.request = types.SimpleNamespace(body=body)
        namespace = dict(asyncio=asyncio, threading=threading, tm=self.manager,
            datetime=datetime, uuid=uuid, os=os, json=json,
            train_utils=types.SimpleNamespace(fix_config_types=lambda config: None,
                get_total_images=lambda path: [], validate_data_dir=lambda path: False),
            normalize_custom_args=lambda config: None,
            apply_sdxl_prediction_type=lambda *args: None,
            apply_anima_training_defaults=lambda *args: None,
            trainer_mapping={'sd-lora': 'unused'}, APIResponseFail=lambda **kwargs: kwargs)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), 'api-handler', 'exec'), namespace)
        self.handler = namespace['create_toml_file']

    async def test_request_is_reserved_before_upload_or_validation(self):
        import asyncio
        job = asyncio.create_task(self.handler(self.request))
        await asyncio.wait_for(self.body_entered.wait(), 2)
        self.assertTrue(self.manager._pending_submissions)
        self.release_body.set()
        result = await asyncio.wait_for(job, 2)
        self.assertIn('message', result)
        self.assertFalse(self.manager._pending_submissions)
        self.manager._shutdown_action.assert_not_called()

    async def test_disconnected_request_releases_reservation(self):
        import asyncio
        job = asyncio.create_task(self.handler(self.request))
        await asyncio.wait_for(self.body_entered.wait(), 2)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        await asyncio.to_thread(eventually, lambda: not self.manager._pending_submissions)
        self.manager._shutdown_action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
