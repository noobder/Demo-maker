"""Installs and starts the user's project, streaming its output to the job log."""
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from urllib.parse import urlparse

import httpx

_job_handle = None


def _kill_on_exit_job():
    """Windows job object that kills every assigned process when this server exits, even if it is killed."""
    global _job_handle
    if _job_handle is None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        _job_handle = (kernel, job)
    return _job_handle


def port_in_use(url: str) -> bool:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        socket.create_connection((parsed.hostname or "localhost", port), timeout=1).close()
        return True
    except OSError:
        return False


class RunError(Exception):
    def __init__(self, message: str, tail: str = "", fixable: bool = True):
        super().__init__(message)
        self.tail = tail
        self.fixable = fixable  # False when changing the run plan cannot help


class AppRunner:
    def __init__(self, cwd, log, venv=None):
        self.cwd = str(cwd)
        self.log = log
        self.venv = venv  # per-project virtualenv; its scripts dir is put first on PATH
        self.proc: subprocess.Popen | None = None
        self.tail: deque[str] = deque(maxlen=40)

    def _popen(self, cmd: str, env: dict) -> subprocess.Popen:
        full_env = {**os.environ, "PYTHONUNBUFFERED": "1", "BROWSER": "none", **env}
        if self.venv:
            scripts = str(self.venv / ("Scripts" if sys.platform == "win32" else "bin"))
            key = next((k for k in full_env if k.upper() == "PATH"), "PATH")
            full_env[key] = scripts + os.pathsep + full_env.get(key, "")
            full_env["VIRTUAL_ENV"] = str(self.venv)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            cmd, shell=True, cwd=self.cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
        if sys.platform == "win32":
            try:
                kernel, job = _kill_on_exit_job()
                kernel.AssignProcessToJobObject(job, int(proc._handle))
            except Exception:
                pass  # best effort; normal cleanup still runs on a clean exit
        return proc

    def _pump(self, proc: subprocess.Popen, prefix: str) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip()
            if line:
                self.tail.append(line)
                self.log(f"[{prefix}] {line[:300]}")

    def run_cmds(self, cmds: list[str], env: dict, fatal: bool = True, timeout: int = 900) -> None:
        for cmd in cmds:
            if not cmd.strip():
                continue
            self.log(f"$ {cmd}")
            self.tail.clear()
            proc = self._popen(cmd, env)
            pump = threading.Thread(target=self._pump, args=(proc, "cmd"), daemon=True)
            pump.start()
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                code = -1
            pump.join(timeout=2)
            if code != 0:
                msg = f"Command failed ({code}): {cmd}"
                if fatal:
                    raise RunError(msg, "\n".join(self.tail))
                self.log(f"Warning: {msg}")

    def start(self, cmd: str, env: dict) -> None:
        if not cmd.strip():
            raise RunError("The run plan has no start command.")
        self.log(f"$ {cmd}  (background)")
        self.tail.clear()
        self.proc = self._popen(cmd, env)
        threading.Thread(target=self._pump, args=(self.proc, "app"), daemon=True).start()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, url: str, timeout: int) -> None:
        self.log(f"Waiting for {url} ...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RunError(f"The app exited with code {self.proc.returncode} before it was ready.", "\n".join(self.tail))
            try:
                httpx.get(url, timeout=2, follow_redirects=True)
                self.log("App is up.")
                return
            except httpx.HTTPError:
                time.sleep(1)
        raise RunError(f"Timed out after {timeout}s waiting for {url}.", "\n".join(self.tail))

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()

    def stop(self) -> None:
        if self.proc is not None:
            self._kill(self.proc)
            self.proc = None
