import subprocess
import sys
import os
import threading
import uuid
import math
import time
from enum import Enum
from typing import Dict, List
from subprocess import Popen, PIPE, TimeoutExpired, CalledProcessError, CompletedProcess
import psutil

from mikazuki.log import log
from mikazuki.train_log_hub import hub

try:
    import msvcrt
    import _winapi
    _mswindows = True
except ModuleNotFoundError:
    _mswindows = False


def kill_proc_tree(pid, including_parent=True):
    parent = psutil.Process(pid)
    children = parent.children(recursive=True)
    targets = children + ([parent] if including_parent else [])
    # Children may exit while we enumerate the tree. Continue killing the rest.
    for process in targets:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, still_alive = psutil.wait_procs(targets, timeout=5)
    if still_alive:
        raise RuntimeError(f"Processes did not exit: {[p.pid for p in still_alive]}")


def shutdown_host():
    """Issue a local power-off request; do not force applications to close."""
    if sys.platform == "win32":
        command = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                "System32", "shutdown.exe"), "/s", "/t", "0"]
    elif sys.platform == "linux":
        command = ["shutdown", "-h", "now"]
    else:
        raise ValueError("当前平台不支持自动关机")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=15, **options)


class TaskStatus(Enum):
    CREATED = 0
    RUNNING = 1
    FINISHED = 2
    TERMINATED = 3
    QUEUED = 4
    FAILED = 5


class Task:
    def __init__(self, task_id, command, environ=None):
        self.task_id = task_id
        self.lock = threading.Lock()
        self.command = command
        self.status = TaskStatus.CREATED
        self.environ = dict(environ if environ is not None else os.environ)
        self.process = None
        self.returncode = None
        self.stdout_thread = None

    def communicate(self, input=None, timeout=None):
        try:
            stdout, stderr = self.process.communicate(input, timeout=timeout)
        except TimeoutExpired as exc:
            self.process.kill()
            if _mswindows:
                exc.stdout, exc.stderr = self.process.communicate()
            else:
                self.process.wait()
            raise
        except:
            self.process.kill()
            raise
        retcode = self.process.poll()
        self.status = TaskStatus.FINISHED
        return CompletedProcess(self.process.args, retcode, stdout, stderr)

    def wait(self):
        self.process.wait()
        if self.stdout_thread is not None:
            self.stdout_thread.join()
        with self.lock:
            self.returncode = self.process.returncode
            if self.status != TaskStatus.TERMINATED:
                self.status = TaskStatus.FINISHED if self.returncode == 0 else TaskStatus.FAILED
        hub.mark_done(self.task_id)

    def _stdout_pump(self):
        """Drain child stdout into TrainLogHub AND echo to parent console."""
        try:
            if not self.process or self.process.stdout is None:
                return
            for line in iter(self.process.stdout.readline, ""):
                hub.append_line(self.task_id, line)
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except Exception:
                    pass
        except Exception as e:
            hub.append_line(self.task_id, f"[stdout pump] {e}")
        finally:
            try:
                if self.process and self.process.stdout:
                    self.process.stdout.close()
            except Exception:
                pass

    def execute(self):
        self.status = TaskStatus.RUNNING
        hub.start_task(self.task_id)
        try:
            self.process = subprocess.Popen(
                self.command,
                env=self.environ,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            hub.append_line(self.task_id, f"[error] Failed to start training process: {e}")
            raise
        self.stdout_thread = threading.Thread(target=self._stdout_pump, daemon=True)
        self.stdout_thread.start()

    def terminate(self):
        with self.lock:
            try:
                kill_proc_tree(self.process.pid, True)
            except psutil.NoSuchProcess:
                if self.process.poll() is None:
                    raise
            self.status = TaskStatus.TERMINATED


class TaskManager:
    """In-memory FIFO scheduler; a slot is released only after process exit."""

    def __init__(self, max_concurrent=1) -> None:
        self.max_concurrent = max_concurrent
        self.tasks: Dict[str, Task] = {}
        self._lock = threading.RLock()
        self._active = set()
        self._pending_submissions = set()
        self._shutdown_enabled = False
        self._shutdown_armed = False
        self._shutdown_timer = None
        self._shutdown_token = None
        self._shutdown_deadline = None
        self._shutdown_error = None
        self._shutdown_triggered = False
        self._shutdown_delay = 60
        self._shutdown_action = shutdown_host

    def create_task(self, command: List[str], environ, name=None):
        with self._lock:
            self._check_accepting_submissions_locked()
            if self._shutdown_enabled:
                self._cancel_shutdown_locked()
                self._shutdown_armed = True
                self._shutdown_error = None
            task_id = str(uuid.uuid4())
            task = Task(task_id, list(command), environ)
            task.name = name or task_id
            task.status = TaskStatus.QUEUED
            hub.start_task(task_id)
            self.tasks[task_id] = task
            self._schedule_locked()
            return task

    def _schedule_locked(self):
        for task in self.tasks.values():
            if len(self._active) >= self.max_concurrent:
                break
            if task.status != TaskStatus.QUEUED or task.task_id in self._active:
                continue
            self._active.add(task.task_id)
            try:
                threading.Thread(target=self._run, args=(task,), daemon=True).start()
            except Exception as exc:
                self._active.discard(task.task_id)
                task.status = TaskStatus.FAILED
                hub.append_line(task.task_id, f"[error] Failed to start worker: {exc}")
                hub.mark_done(task.task_id)
        self._maybe_shutdown_locked()

    def _run(self, task):
        try:
            with self._lock:
                if task.status == TaskStatus.TERMINATED:
                    return
                task.execute()
            task.wait()
        except Exception as exc:
            # If starting the stdout reader failed, Popen may already be alive.
            # Reap it before releasing the slot to the next training job.
            if task.process is not None and task.process.poll() is None:
                try:
                    kill_proc_tree(task.process.pid, True)
                    task.process.wait()
                except Exception as cleanup_error:
                    log.error(f"Unable to stop task {task.task_id}: {cleanup_error}")
                    task.process.wait()
            with self._lock:
                if task.status != TaskStatus.TERMINATED:
                    task.status = TaskStatus.FAILED
            hub.append_line(task.task_id, f"[error] {exc}")
            hub.mark_done(task.task_id)
            log.error(f"Training task {task.task_id} failed: {exc}")
        finally:
            with self._lock:
                self._active.discard(task.task_id)
                self._schedule_locked()

    def add_task(self, task_id: str, task: Task):
        with self._lock:
            self.tasks[task_id] = task

    def terminate_task(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return False
            if task.status == TaskStatus.QUEUED:
                task.status = TaskStatus.TERMINATED
                hub.append_line(task_id, "[cancelled] 已取消排队任务")
                hub.mark_done(task_id)
            elif task.status == TaskStatus.RUNNING:
                task.terminate()
            self._maybe_shutdown_locked()
            return True

    def _check_accepting_submissions_locked(self):
        if self._shutdown_triggered:
            raise RuntimeError("关机命令已发送，不能再提交训练任务；如系统未关机，请重启训练服务后再提交。")

    def begin_submission(self, submission):
        """Protect slow request validation/upload before a job reaches the queue."""
        with self._lock:
            # HTTP cancellation may release this token before its worker starts.
            if submission.is_set():
                return
            self._check_accepting_submissions_locked()
            self._pending_submissions.add(submission)
            self._cancel_shutdown_locked()

    def end_submission(self, submission):
        submission.set()
        with self._lock:
            self._pending_submissions.discard(submission)
            self._maybe_shutdown_locked()

    def _cancel_shutdown_locked(self):
        if self._shutdown_timer is not None:
            self._shutdown_timer.cancel()
        self._shutdown_timer = None
        self._shutdown_token = None
        self._shutdown_deadline = None

    def set_shutdown(self, enabled: bool):
        with self._lock:
            if self._shutdown_triggered:
                raise ValueError("关机命令已发送，倒计时已结束，无法取消或重新启用自动关机。")
            if enabled and sys.platform not in {"win32", "linux"}:
                raise ValueError("当前平台不支持自动关机")
            if enabled != self._shutdown_enabled:
                self._cancel_shutdown_locked()
                self._shutdown_enabled = enabled
                self._shutdown_error = None
                self._shutdown_triggered = False
                # Enabling on an idle server waits for a NEW submission.
                self._shutdown_armed = enabled and bool(self._active or any(
                    task.status == TaskStatus.QUEUED for task in self.tasks.values()))
                self._maybe_shutdown_locked()
            return self.shutdown_status()

    def shutdown_status(self):
        with self._lock:
            remaining = (max(0, math.ceil(self._shutdown_deadline - time.monotonic()))
                         if self._shutdown_deadline is not None else None)
            return {
                "supported": sys.platform in {"win32", "linux"},
                "enabled": self._shutdown_enabled,
                "remaining_seconds": remaining,
                "delay_seconds": self._shutdown_delay,
                "triggered": self._shutdown_triggered,
                "error": self._shutdown_error,
            }

    def _queue_busy_locked(self):
        return bool(self._pending_submissions or self._active or any(task.status == TaskStatus.QUEUED
                                       for task in self.tasks.values()))

    def _maybe_shutdown_locked(self):
        if (not self._shutdown_enabled or not self._shutdown_armed
                or self._shutdown_timer is not None or self._queue_busy_locked()):
            return
        token = object()
        try:
            timer = threading.Timer(self._shutdown_delay, self._finish_shutdown, args=(token,))
            timer.daemon = True
            self._shutdown_token = token
            self._shutdown_timer = timer
            self._shutdown_deadline = time.monotonic() + self._shutdown_delay
            timer.start()
        except Exception as exc:
            self._cancel_shutdown_locked()
            self._shutdown_enabled = False
            self._shutdown_armed = False
            self._shutdown_error = f"无法启动关机倒计时：{exc}"

    def _finish_shutdown(self, token):
        with self._lock:
            # A cancelled timer can still enter its callback. Check its identity.
            if token is not self._shutdown_token or not self._shutdown_enabled:
                return
            self._cancel_shutdown_locked()
            if self._queue_busy_locked():
                return
            self._shutdown_enabled = False
            self._shutdown_armed = False
            try:
                self._shutdown_action()
                self._shutdown_triggered = True
            except Exception as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                self._shutdown_error = f"自动关机失败：{detail}"
                log.error(self._shutdown_error)

    def wait_for_process(self, task_id: str):
        if task_id in self.tasks:
            self.tasks[task_id].wait()

    def dump(self) -> List[Dict]:
        with self._lock:
            position = 0
            result = []
            for task in self.tasks.values():
                queued = task.status == TaskStatus.QUEUED
                if queued:
                    position += 1
                result.append({
                    "id": task.task_id,
                    "name": getattr(task, "name", task.task_id),
                    "status": task.status.name,
                    "queue_position": position if queued else None,
                    "returncode": task.returncode,
                    "config_file": (task.command[task.command.index("--config_file") + 1]
                                    if "--config_file" in task.command
                                    and task.command.index("--config_file") + 1 < len(task.command) else None),
                })
            return result


tm = TaskManager()
