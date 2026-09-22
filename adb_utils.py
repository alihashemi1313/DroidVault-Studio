import cmd
import os
import sys
if getattr(sys, "frozen", False):
    SCRIPT_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

import hashlib
import io
import json
import queue
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from collections import deque
from pathlib import PurePosixPath

from PIL import Image

POPEN_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0

COMMAND_TIMEOUT = 30
INSTALL_TIMEOUT = 300
TRANSFER_IDLE_TIMEOUT = 120
STREAM_IDLE_TIMEOUT = 90
MAX_CAPTURE_BYTES = 64 * 1024
DEVICE_LOCK_PATH = "/data/local/tmp/droidvault_operation.lock"

_ACTIVE_PROCESSES = {}
_ACTIVE_PROCESSES_LOCK = threading.Lock()

# Global PATH Variables
ADB_PATH = "adb"
SCRCPY_PATH = "scrcpy"
_FOUND_SCRCPY = None
_FOUND_ADB = None


def _bundled_tool_dirs(platform_name=None, script_dir=None):
    platform_name = platform_name or ("windows" if sys.platform.startswith("win") else "linux")
    script_dir = script_dir or SCRIPT_DIR
    dirs = [
        os.path.join(script_dir, "tools", platform_name),
        os.path.join(script_dir, "platform-tools"),
    ]
    if platform_name == "windows":
        dirs.append(os.path.join(script_dir, "scrcpy-win64-v4.1"))
    return tuple(dirs)


def _find_scrcpy_and_adb():
    global ADB_PATH, SCRCPY_PATH, _FOUND_SCRCPY, _FOUND_ADB

    is_win = sys.platform.startswith("win")
    adb_bin = "adb.exe" if is_win else "adb"
    scrcpy_bin = "scrcpy.exe" if is_win else "scrcpy"

    found_scrcpy_path = None
    found_adb_path = None

    # 1. Checking local paths relative to the executable or PyInstaller bundle
    candidate_dirs = [SCRIPT_DIR] + list(_bundled_tool_dirs())

    for d in candidate_dirs:
        adb_candidate = os.path.join(d, adb_bin)
        scrcpy_candidate = os.path.join(d, scrcpy_bin)

        if (
            not found_adb_path
            and os.path.isfile(adb_candidate)
            and (is_win or os.access(adb_candidate, os.X_OK))
        ):
            found_adb_path = adb_candidate

        if (
            not found_scrcpy_path
            and os.path.isfile(scrcpy_candidate)
            and (is_win or os.access(scrcpy_candidate, os.X_OK))
        ):
            found_scrcpy_path = scrcpy_candidate

    # 2. Checking System Variables (PATH)
    if not found_adb_path:
        found_adb_path = shutil.which(adb_bin)

    if not found_scrcpy_path:
        found_scrcpy_path = shutil.which(scrcpy_bin)

    # Configuring Global Variables
    _FOUND_ADB = found_adb_path
    _FOUND_SCRCPY = found_scrcpy_path

    ADB_PATH = found_adb_path if found_adb_path else adb_bin
    SCRCPY_PATH = found_scrcpy_path if found_scrcpy_path else scrcpy_bin

    return _FOUND_SCRCPY, _FOUND_ADB


# Called during module import to finalize variable preparation
_FOUND_SCRCPY, _FOUND_ADB = _find_scrcpy_and_adb()

def _find_local_zstd():
    env = os.environ.get("ZSTD_PATH")
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    for name in ("zstd.exe", "zstd"):
        candidate = os.path.join(SCRIPT_DIR, name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return shutil.which("zstd")


ZSTD_PATH = _find_local_zstd()

CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")
ICON_CACHE_DIR = os.path.join(CACHE_DIR, "icons")
os.makedirs(ICON_CACHE_DIR, exist_ok=True)

BLOATWARE_PRESETS = {
    "com.samsung.android.bixby.agent": {"vendor": "Samsung", "level": "Safe", "desc": "Bixby Voice Assistant Core"},
    "com.samsung.android.bixby.service": {"vendor": "Samsung", "level": "Safe", "desc": "Bixby Background Daemon"},
    "com.samsung.android.app.spage": {"vendor": "Samsung", "level": "Safe", "desc": "Bixby Home / Samsung Free Feed"},
    "com.samsung.android.aremoji": {"vendor": "Samsung", "level": "Safe", "desc": "AR Emoji Studio"},
    "com.samsung.android.arzone": {"vendor": "Samsung", "level": "Safe", "desc": "Augmented Reality Zone"},
    "com.samsung.android.kidsinstaller": {"vendor": "Samsung", "level": "Safe", "desc": "Samsung Kids Mode Installer"},
    "com.sec.android.app.billing": {"vendor": "Samsung", "level": "Safe", "desc": "Galaxy Store In-App Billing"},
    "com.facebook.katana": {"vendor": "Samsung", "level": "Safe", "desc": "Pre-installed Facebook App"},
    "com.facebook.system": {"vendor": "Samsung", "level": "Safe", "desc": "Facebook Installer Daemon"},
    "com.facebook.appmanager": {"vendor": "Samsung", "level": "Safe", "desc": "Facebook Manager"},
    "com.facebook.services": {"vendor": "Samsung", "level": "Safe", "desc": "Facebook Background Services"},
    "com.samsung.android.game.gamehome": {"vendor": "Samsung", "level": "Recommended", "desc": "Gaming Hub / Game Launcher"},
    "com.samsung.android.game.gametools": {"vendor": "Samsung", "level": "Recommended", "desc": "Game Tools Floating Widget"},
    "com.samsung.android.dexpad": {"vendor": "Samsung", "level": "Advanced", "desc": "Samsung DeX Environment"},
    "com.miui.analytics": {"vendor": "Xiaomi", "level": "Safe", "desc": "Tracking & Analytics"},
    "com.miui.msa.global": {"vendor": "Xiaomi", "level": "Safe", "desc": "MIUI System Ad Services"},
    "com.miui.bugreport": {"vendor": "Xiaomi", "level": "Safe", "desc": "Bug Reporting Tool"},
    "com.miui.yellowpage": {"vendor": "Xiaomi", "level": "Safe", "desc": "Yellow Page Directory Ads"},
    "com.xiaomi.midrop": {"vendor": "Xiaomi", "level": "Recommended", "desc": "ShareMe File Transfer"},
    "com.google.android.apps.tachyon": {"vendor": "Google", "level": "Safe", "desc": "Google Meet / Duo"},
    "com.google.android.feedback": {"vendor": "Google", "level": "Safe", "desc": "Crash Feedback Diagnostic"},
    "com.google.android.apps.wellbeing": {"vendor": "Google", "level": "Recommended", "desc": "Digital Wellbeing Tracker"},
}


class AdbError(Exception):
    def __init__(self, message, stdout="", stderr=""):
        super().__init__(message)
        self.stdout = stdout or ""
        self.stderr = stderr or ""


class OperationCancelled(AdbError):
    pass


def _register_process(proc, serial=None):
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES[proc] = serial


def _unregister_process(proc):
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.pop(proc, None)


def _terminate_process(proc, grace=2.0):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


def terminate_active_processes(serial=None):
    with _ACTIVE_PROCESSES_LOCK:
        procs = [p for p, s in _ACTIVE_PROCESSES.items() if serial is None or s == serial]
    for proc in procs:
        _terminate_process(proc)


def _run(args, timeout=None):
    if timeout is None:
        timeout = COMMAND_TIMEOUT
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=POPEN_FLAGS,
        )
    except FileNotFoundError:
        raise AdbError(f"ADB binary not found: {args[0]}")
    except subprocess.TimeoutExpired:
        raise AdbError(f"Command timed out after {timeout}s: {' '.join(args)}")

def adb(serial, args, timeout=None, check=True):
    full = [ADB_PATH]
    if serial:
        full += ["-s", serial]
    full += args
    proc = _run(full, timeout=timeout)
    if check and proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise AdbError(f"ADB command failed: {' '.join(args)}\n{err}", proc.stdout or "", proc.stderr or "")
    return proc


def adb_shell(serial, command, root=False, timeout=None, check=True):
    remote_cmd = "su -c " + shlex.quote(command) if root else command
    proc = adb(serial, ["shell", remote_cmd], timeout=timeout, check=False)
    if check and proc.returncode != 0:
        err = ((proc.stderr or "").strip() + "\n" + (proc.stdout or "").strip()).strip()
        raise AdbError(f"Shell command failed: {command}\n{err}", proc.stdout or "", proc.stderr or "")
    return proc


def _bounded_reader(pipe, activity_queue, sink=None, chunk_size=4096):
    try:
        while True:
            chunk = pipe.read(chunk_size)
            if not chunk:
                break
            if sink is not None:
                sink.append(chunk)
            try:
                activity_queue.put_nowait(("activity", len(chunk)))
            except queue.Full:
                pass
    except Exception:
        pass


def _run_process_cancellable(args, serial=None, should_cancel=lambda: False,
                             idle_timeout=TRANSFER_IDLE_TIMEOUT, total_timeout=None,
                             stdin_writer=None, safety_check=None):
    """Run a process without unbounded capture; terminate on cancel or a real stall."""
    stderr_buf = deque(maxlen=32)
    stdout_buf = deque(maxlen=32)
    activity = queue.Queue(maxsize=128)
    start = time.monotonic()
    last_activity = start
    proc = None
    writer_error = []
    writer_done = threading.Event()
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin_writer else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=POPEN_FLAGS,
        )
        _register_process(proc, serial)
        t_out = threading.Thread(target=_bounded_reader, args=(proc.stdout, activity, stdout_buf), daemon=True)
        t_err = threading.Thread(target=_bounded_reader, args=(proc.stderr, activity, stderr_buf), daemon=True)
        t_out.start()
        t_err.start()

        if stdin_writer:
            def do_write():
                try:
                    stdin_writer(proc.stdin, activity)
                except Exception as exc:
                    writer_error.append(exc)
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                    writer_done.set()
            threading.Thread(target=do_write, daemon=True).start()
        else:
            writer_done.set()

        while proc.poll() is None:
            if should_cancel():
                _terminate_process(proc)
                raise OperationCancelled("Operation cancelled by user.")
            if safety_check:
                reason = safety_check()
                if reason:
                    _terminate_process(proc)
                    raise AdbError(str(reason))
            now = time.monotonic()
            if total_timeout and now - start > total_timeout:
                _terminate_process(proc)
                raise AdbError(f"Command exceeded total timeout ({total_timeout}s).")
            try:
                activity.get(timeout=0.25)
                last_activity = time.monotonic()
            except queue.Empty:
                pass
            if idle_timeout and time.monotonic() - last_activity > idle_timeout:
                _terminate_process(proc)
                raise AdbError(f"ADB transfer stalled for more than {idle_timeout}s.")

        writer_done.wait(timeout=2)

        def joined(buf):
            data = b"".join(buf)
            if len(data) > MAX_CAPTURE_BYTES:
                data = data[-MAX_CAPTURE_BYTES:]
            return data.decode("utf-8", errors="replace")

        stdout_text = joined(stdout_buf)
        stderr_text = joined(stderr_buf)
        # If the child failed, its stderr is the primary cause. A BrokenPipe in
        # the producer is normally just a consequence of the remote tar exiting.
        if proc.returncode != 0:
            detail = (stderr_text or stdout_text).strip()
            if writer_error:
                detail = (detail + f"\n[stream writer: {writer_error[0]}]").strip()
            return subprocess.CompletedProcess(args, proc.returncode, stdout_text, detail)
        if writer_error:
            raise AdbError(f"Failed while streaming data: {writer_error[0]}")
        return subprocess.CompletedProcess(args, proc.returncode, stdout_text, stderr_text)
    finally:
        if proc is not None:
            _unregister_process(proc)


def _capture_process_cancellable(args, serial=None, should_cancel=lambda: False,
                                total_timeout=300, max_output_bytes=16 * 1024 * 1024):
    proc = None
    stdout_chunks = []
    stderr_chunks = []
    out_size = 0
    err_size = 0
    out_lock = threading.Lock()
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=POPEN_FLAGS,
        )
        _register_process(proc, serial)

        def reader(pipe, target, is_stdout):
            nonlocal out_size, err_size
            try:
                while True:
                    chunk = pipe.read(8192)
                    if not chunk:
                        break
                    with out_lock:
                        if is_stdout:
                            if out_size < max_output_bytes:
                                keep = chunk[:max_output_bytes - out_size]
                                target.append(keep)
                                out_size += len(keep)
                        else:
                            if err_size < max_output_bytes:
                                keep = chunk[:max_output_bytes - err_size]
                                target.append(keep)
                                err_size += len(keep)
            except Exception:
                pass

        t1 = threading.Thread(target=reader, args=(proc.stdout, stdout_chunks, True), daemon=True)
        t2 = threading.Thread(target=reader, args=(proc.stderr, stderr_chunks, False), daemon=True)
        t1.start(); t2.start()
        while proc.poll() is None:
            if should_cancel():
                _terminate_process(proc)
                raise OperationCancelled("Operation cancelled by user.")
            if total_timeout and time.monotonic() - start > total_timeout:
                _terminate_process(proc)
                raise AdbError(f"Command exceeded total timeout ({total_timeout}s).")
            time.sleep(0.2)
        t1.join(timeout=1); t2.join(timeout=1)
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    finally:
        if proc is not None:
            _unregister_process(proc)


def check_adb_available():
    try:
        return _run([ADB_PATH, "version"], timeout=10).returncode == 0
    except Exception:
        return False


def adb_pair(ip_port, code):
    proc = _run([ADB_PATH, "pair", ip_port, code], timeout=15)
    return proc.returncode == 0, (proc.stdout or "").strip() or (proc.stderr or "").strip()


def adb_connect(ip_port):
    proc = _run([ADB_PATH, "connect", ip_port], timeout=15)
    out = (proc.stdout or "").strip()
    return "connected to" in out.lower(), out


def adb_tcpip(serial, port=5555):
    proc = adb(serial, ["tcpip", str(port)], timeout=15, check=False)
    return proc.returncode == 0


def get_free_space_mb(serial, path):
    q = shlex.quote(path)
    proc = adb_shell(serial, f"stat -f -c '%a %S' {q} 2>/dev/null", check=False, timeout=15)
    out = (proc.stdout or "").strip()
    if out:
        parts = out.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int((int(parts[0]) * int(parts[1])) / (1024 * 1024))

    proc = adb_shell(serial, f"df -k {q} 2>/dev/null", check=False, timeout=15)
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    for line in reversed(lines):
        parts = line.split()
        # POSIX df: Filesystem 1K-blocks Used Available Use% Mounted on
        if len(parts) >= 4 and parts[-3].isdigit():
            return int(int(parts[-3]) / 1024)
    return 0


def get_free_storage_mb(serial):
    return get_free_space_mb(serial, "/data")


def get_remote_size_bytes(serial, remote_path, root=True):
    proc = adb_shell(serial, f"du -sk {shlex.quote(remote_path)} 2>/dev/null | head -n 1",
                     root=root, check=False, timeout=45)
    out = (proc.stdout or "").strip().split()
    if out and out[0].isdigit():
        return int(out[0]) * 1024
    return 0


def get_external_sdcard_path(serial):
    proc = adb_shell(serial, "ls -1 /storage 2>/dev/null", check=False, timeout=15)
    for line in (proc.stdout or "").splitlines():
        name = line.strip()
        if re.match(r"^[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}$", name):
            full_p = f"/storage/{name}"
            if path_exists(serial, full_p, root=True):
                return full_p

    proc_m = adb_shell(serial, "cat /proc/mounts 2>/dev/null", check=False, timeout=15)
    for line in (proc_m.stdout or "").splitlines():
        if "/storage/" in line and "emulated" not in line and "self" not in line:
            parts = line.split()
            if len(parts) >= 2:
                mp = parts[1]
                if re.search(r"/storage/[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}", mp):
                    return mp
    return None


def path_exists(serial, remote_path, root=True):
    proc = adb_shell(serial, f"test -e {shlex.quote(remote_path)}", root=root, check=False, timeout=15)
    return proc.returncode == 0


def list_dir(serial, remote_path, root=True, strict=False):
    proc = adb_shell(serial, f"ls -1A {shlex.quote(remote_path)}", root=root, check=False, timeout=30)
    if proc.returncode != 0:
        if strict:
            raise AdbError(f"Could not list {remote_path}: {(proc.stderr or proc.stdout or '').strip()[:500]}")
        return []
    if not proc.stdout:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def remove_remote(serial, remote_path, root=True):
    return adb_shell(serial, f"rm -rf -- {shlex.quote(remote_path)}", root=root, check=False, timeout=30)


def cleanup_stale_temp(serial, older_than_hours=24):
    """Only clean DroidVault-owned temporary files; never wildcard unrelated /data/local/tmp files."""
    minutes = max(60, int(older_than_hours * 60))
    cmd = (
        "mkdir -p /data/local/tmp/droidvault 2>/dev/null; "
        f"find /data/local/tmp/droidvault -mindepth 1 -maxdepth 1 -mmin +{minutes} "
        "\\( -name '*.tmp' -o -name '*.part' \\) -delete 2>/dev/null; true"
    )
    return adb_shell(serial, cmd, root=True, check=False, timeout=20)


def emergency_clean_temp(serial):
    # Backward-compatible name, now intentionally non-aggressive.
    return cleanup_stale_temp(serial)


def cleanup_stale_storage_temp(serial, remote_root, older_than_seconds=0):
    """Recover only DroidVault-owned storage transactions, then remove empty temp namespaces."""
    if not remote_root:
        return []
    root = remote_root.rstrip("/") or "/"
    qroot = shlex.quote(root)
    age = max(0, int(older_than_seconds))
    cmd = (
        f"ROOT={qroot}; NOW=$(date +%s); "
        "for P in \"$ROOT\"/.droidvault_tmp_*; do "
        "[ -d \"$P\" ] || continue; "
        "MT=$(stat -c %Y \"$P\" 2>/dev/null || echo 0); "
        f"if [ {age} -eq 0 ] || ([ \"$MT\" -gt 0 ] && [ $((NOW-MT)) -gt {age} ]); then printf '%s\\n' \"$P\"; fi; "
        "done"
    )
    proc = adb_shell(serial, cmd, root=False, check=False, timeout=30)
    recovered = []
    for temp_dir in [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]:
        # Directory names are generated by us and must stay directly below root.
        if not temp_dir.startswith(root + "/.droidvault_tmp_"):
            continue
        entries = list_dir(serial, temp_dir, root=False, strict=False)
        unresolved = False
        for name in entries:
            if not (name.startswith("txn_") and name.endswith(".json")):
                continue
            journal_path = f"{temp_dir}/{name}"
            cat = adb_shell(serial, f"cat {shlex.quote(journal_path)}", root=False, check=False, timeout=15)
            try:
                data = json.loads((cat.stdout or "").strip())
            except Exception:
                unresolved = True
                continue
            target = str(data.get("target") or "")
            rollback = str(data.get("rollback") or "")
            old_exists = bool(data.get("old_exists", True))
            prefix = root + "/"
            # Never trust shared-storage journal contents without strict lexical
            # namespace validation; the journal itself lives on user-writable storage.
            target_parts = PurePosixPath(target).parts
            root_parts = PurePosixPath(root).parts
            rollback_parts = PurePosixPath(rollback).parts
            target_under_root = (
                target.startswith(prefix)
                and ".." not in target_parts
                and target_parts[:len(root_parts)] == root_parts
            )
            rollback_safe = (
                rollback.startswith(target + ".droidvault_rollback_")
                and ".." not in rollback_parts
            )
            if not target_under_root or not rollback_safe:
                unresolved = True
                continue
            target_exists = path_exists(serial, target, root=False)
            rollback_exists = path_exists(serial, rollback, root=False)
            resolved = True
            if rollback_exists and target_exists:
                rm = adb_shell(serial, f"rm -f -- {shlex.quote(rollback)}", root=False, check=False, timeout=30)
                resolved = rm.returncode == 0
            elif rollback_exists and not target_exists:
                mv = adb_shell(serial, f"mv {shlex.quote(rollback)} {shlex.quote(target)}", root=False, check=False, timeout=30)
                resolved = mv.returncode == 0
                if resolved:
                    recovered.append(target)
            elif not rollback_exists:
                if old_exists and not target_exists:
                    # An original file was expected, but neither copy is present.
                    resolved = False
                else:
                    # No rollback is needed (new file), or commit completed and its
                    # rollback was already deleted before journal cleanup.
                    resolved = True
            if resolved:
                adb_shell(serial, f"rm -f -- {shlex.quote(journal_path)}", root=False, check=False, timeout=15)
            else:
                unresolved = True
        # Preserve unknown/unresolved journals. Non-journal .part files are safe
        # to discard because they were never committed to a destination name.
        remaining = list_dir(serial, temp_dir, root=False, strict=False)
        journals_left = [n for n in remaining if n.startswith("txn_") and n.endswith(".json")]
        if not journals_left and not unresolved:
            adb_shell(serial, f"rm -rf -- {shlex.quote(temp_dir)}", root=False, check=False, timeout=30)
        else:
            # Clean only never-committed .part files while preserving recovery metadata.
            adb_shell(serial, f"find {shlex.quote(temp_dir)} -maxdepth 1 -type f -name '*.part' -delete 2>/dev/null; true",
                      root=False, check=False, timeout=20)
    return recovered


def acquire_device_operation_lock(serial, operation_id, stale_seconds=600):
    op = re.sub(r"[^A-Za-z0-9_.-]", "_", str(operation_id))[:100]
    lock = shlex.quote(DEVICE_LOCK_PATH)
    owner = shlex.quote(op)
    cmd = (
        f"LOCK={lock}; NOW=$(date +%s); "
        "if mkdir \"$LOCK\" 2>/dev/null; then "
        f"printf %s {owner} > \"$LOCK/owner\"; exit 0; fi; "
        "MT=$(stat -c %Y \"$LOCK\" 2>/dev/null || echo 0); "
        f"if [ \"$MT\" -gt 0 ] && [ $((NOW-MT)) -gt {int(stale_seconds)} ]; then "
        "rm -rf \"$LOCK\" 2>/dev/null; "
        "if mkdir \"$LOCK\" 2>/dev/null; then "
        f"printf %s {owner} > \"$LOCK/owner\"; exit 0; fi; fi; exit 23"
    )
    proc = adb_shell(serial, cmd, root=False, check=False, timeout=15)
    return proc.returncode == 0


def release_device_operation_lock(serial, operation_id):
    op = re.sub(r"[^A-Za-z0-9_.-]", "_", str(operation_id))[:100]
    cmd = (
        f"LOCK={shlex.quote(DEVICE_LOCK_PATH)}; "
        "CUR=$(cat \"$LOCK/owner\" 2>/dev/null || true); "
        f"[ \"$CUR\" = {shlex.quote(op)} ] && rm -rf \"$LOCK\"; true"
    )
    return adb_shell(serial, cmd, root=False, check=False, timeout=15)


def start_device_lock_heartbeat(serial, operation_id, interval=30):
    stop_event = threading.Event()
    op = re.sub(r"[^A-Za-z0-9_.-]", "_", str(operation_id))[:100]

    def worker():
        while not stop_event.wait(interval):
            try:
                cmd = (
                    f"LOCK={shlex.quote(DEVICE_LOCK_PATH)}; "
                    "CUR=$(cat \"$LOCK/owner\" 2>/dev/null || true); "
                    f"[ \"$CUR\" = {shlex.quote(op)} ] && touch \"$LOCK\"; true"
                )
                adb_shell(serial, cmd, root=False, check=False, timeout=10)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
    return stop_event


def stop_device_lock_heartbeat(stop_event):
    if stop_event is not None:
        stop_event.set()


def is_device_operation_locked(serial):
    proc = adb_shell(serial, f"test -d {shlex.quote(DEVICE_LOCK_PATH)}", root=False, check=False, timeout=10)
    return proc.returncode == 0


def list_devices():
    proc = adb(None, ["devices", "-l"], check=True, timeout=15)
    devices = []
    for line in (proc.stdout or "").splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def get_prop(serial, prop):
    proc = adb_shell(serial, f"getprop {shlex.quote(prop)}", check=False, timeout=10)
    return (proc.stdout or "").strip()


def get_device_info(serial):
    return {
        "serial": serial,
        "model": get_prop(serial, "ro.product.model") or "Android Device",
        "manufacturer": get_prop(serial, "ro.product.manufacturer") or "",
        "android_version": get_prop(serial, "ro.build.version.release") or "?",
        "sdk": get_prop(serial, "ro.build.version.sdk") or "?",
        "fingerprint": get_prop(serial, "ro.build.fingerprint") or "",
    }


def check_root(serial):
    try:
        proc = adb_shell(serial, "id", root=True, check=False, timeout=15)
        return "uid=0" in (proc.stdout or "")
    except Exception:
        return False


def list_packages_fast(serial):
    cmd = "pm list packages -3; echo '___SPLIT___'; pm list packages -s; echo '___SPLIT___'; pm list packages -d"
    proc = adb_shell(serial, cmd, check=False, timeout=45)
    raw = proc.stdout or ""
    parts = raw.split("___SPLIT___")
    user_raw = parts[0] if len(parts) > 0 else ""
    sys_raw = parts[1] if len(parts) > 1 else ""
    dis_raw = parts[2] if len(parts) > 2 else ""
    user_pkgs = {line[8:].strip() for line in user_raw.splitlines() if line.startswith("package:")}
    system_pkgs = {line[8:].strip() for line in sys_raw.splitlines() if line.startswith("package:")}
    disabled_pkgs = {line[8:].strip() for line in dis_raw.splitlines() if line.startswith("package:")}
    return [
        {"package": pkg, "is_system": pkg in system_pkgs and pkg not in user_pkgs, "is_disabled": pkg in disabled_pkgs}
        for pkg in sorted(user_pkgs | system_pkgs, key=str.lower)
    ]


def get_batch_data_sizes(serial, should_cancel=lambda: False):
    # Preserve the Data Size feature, but keep it low-priority, cancellable and bounded.
    cmd = (
        "if command -v ionice >/dev/null 2>&1; then "
        "ionice -c 3 nice -n 19 du -sk /data/data/* 2>/dev/null; "
        "else nice -n 19 du -sk /data/data/* 2>/dev/null || du -sk /data/data/* 2>/dev/null; fi"
    )
    remote_cmd = "su -c " + shlex.quote(cmd)
    proc = _capture_process_cancellable(
        [ADB_PATH, "-s", serial, "shell", remote_cmd], serial=serial,
        should_cancel=should_cancel, total_timeout=300,
    )
    sizes = {}
    if proc.returncode != 0:
        return sizes
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            sizes[os.path.basename(parts[1].rstrip("/"))] = int(parts[0]) * 1024
    return sizes


def get_batch_apk_sizes(serial, should_cancel=lambda: False):
    # One device-side pass maps the real package name to its install directory,
    # then sums base + split APKs. It is cancellable so a background size scan
    # cannot keep competing with a real backup/restore operation.
    cmd = "\n".join([
        "pm list packages -f 2>/dev/null | while IFS= read -r L; do",
        "  case \"$L\" in package:*=*)",
        "    P=\"${L#package:}\"; PKG=\"${P##*=}\"; APK=\"${P%=*}\"; DIR=\"${APK%/*}\"",
        "    TOTAL=0",
        "    for F in \"$DIR\"/*.apk; do",
        "      [ -f \"$F\" ] || continue",
        "      S=$(stat -c %s \"$F\" 2>/dev/null || echo 0)",
        "      case \"$S\" in ''|*[!0-9]*) S=0;; esac",
        "      TOTAL=$((TOTAL + S))",
        "    done",
        "    printf '%s %s\\n' \"$TOTAL\" \"$PKG\"",
        "  ;; esac",
        "done",
    ])
    remote_cmd = "su -c " + shlex.quote(cmd)
    proc = _capture_process_cancellable(
        [ADB_PATH, "-s", serial, "shell", remote_cmd], serial=serial,
        should_cancel=should_cancel, total_timeout=120,
    )
    sizes = {}
    if proc.returncode != 0:
        return sizes
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1]:
            sizes[parts[1].strip()] = int(parts[0])
    return sizes


def format_size(size_bytes):
    if not size_bytes or size_bytes <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    size = float(size_bytes)
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.1f} {units[idx]}"


def calculate_sha256(file_path):
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def extract_icon_from_local_apk(apk_path, package_name):
    cached = os.path.join(ICON_CACHE_DIR, f"{package_name}.png")
    if os.path.exists(cached) and os.path.getsize(cached) > 200:
        return cached
    if not os.path.isfile(apk_path):
        return None
    try:
        with zipfile.ZipFile(apk_path, "r") as z:
            names = [n for n in z.namelist() if n.lower().startswith(("res/", "resources/"))]
            blacklist = (
                "notification", "notify", "status", "small", "badge", "btn", "button", "action", "menu",
                "tab", "toolbar", "back", "arrow", "camera", "video", "mic", "audio", "edit", "delete",
                "share", "close", "abc_", "material_", "common_", "googleg_", "bg_", "background", "shadow",
                "mask", "vector", "dialog", "placeholder", "dummy"
            )
            pkg_keywords = [
                p for p in package_name.lower().split(".")
                if p not in ("com", "org", "net", "io", "app", "android", "mobile", "helper", "prod")
            ]
            candidates = []
            for n in names:
                lower = n.lower()
                if not (lower.endswith(".png") or lower.endswith(".webp")):
                    continue
                fn = os.path.splitext(os.path.basename(lower))[0]
                if any(bad in fn for bad in blacklist if bad not in package_name.lower()):
                    continue
                score = 0
                if any(kw in fn for kw in pkg_keywords if len(kw) >= 3):
                    score += 160
                if fn in ("ic_launcher", "ic_launcher_round", "app_icon", "appicon"):
                    score += 130
                elif "product_logo" in fn or "fre_product_logo" in fn:
                    score += 140
                elif fn.startswith("ic_launcher") or fn.startswith("app_icon"):
                    score += 100
                elif fn in ("icon", "logo", "launcher") and "mipmap" in lower:
                    score += 80
                if "/mipmap" in lower:
                    score += 90
                elif "/drawable" in lower:
                    score += 20
                if "xxxhdpi" in lower:
                    score += 50
                elif "xxhdpi" in lower:
                    score += 40
                elif "xhdpi" in lower:
                    score += 30
                elif "hdpi" in lower:
                    score += 15
                if score > 50:
                    candidates.append((score, n))
            candidates.sort(key=lambda x: x[0], reverse=True)
            for _, cand_name in candidates[:20]:
                try:
                    img = Image.open(io.BytesIO(z.read(cand_name))).convert("RGBA")
                    w, h = img.size
                    if abs(w - h) > 2 or w < 32 or w > 1024:
                        continue
                    extrema = img.getextrema()
                    if extrema[3][1] < 20:
                        continue
                    if all(extrema[i][0] == extrema[i][1] for i in range(3)):
                        continue
                    img.resize((48, 48), Image.Resampling.LANCZOS).save(cached, format="PNG")
                    return cached
                except Exception:
                    continue
    except Exception:
        pass
    return None


def get_granted_permissions(serial, package):
    proc = adb_shell(serial, f"dumpsys package {shlex.quote(package)}", check=False, timeout=30)
    granted = set()
    in_runtime = False
    base_indent = None
    for raw in (proc.stdout or "").splitlines():
        stripped = raw.strip()
        lower = stripped.lower()
        if lower.startswith("runtime permissions:"):
            in_runtime = True
            base_indent = len(raw) - len(raw.lstrip())
            continue
        if in_runtime:
            indent = len(raw) - len(raw.lstrip())
            if stripped and base_indent is not None and indent <= base_indent and not stripped.startswith("android.permission."):
                in_runtime = False
                continue
            if stripped.startswith("android.permission.") and "granted=true" in lower:
                granted.add(stripped.split(":", 1)[0].strip())
    return sorted(granted)


def grant_package_permission(serial, package, permission):
    cmd = f"pm grant {shlex.quote(package)} {shlex.quote(permission)}"
    proc = adb_shell(serial, cmd, root=False, check=False, timeout=15)
    if proc.returncode == 0:
        return True
    proc = adb_shell(serial, cmd, root=True, check=False, timeout=15)
    return proc.returncode == 0


def revoke_package_permission(serial, package, permission):
    cmd = f"pm revoke {shlex.quote(package)} {shlex.quote(permission)}"
    proc = adb_shell(serial, cmd, root=False, check=False, timeout=15)
    if proc.returncode == 0:
        return True
    proc = adb_shell(serial, cmd, root=True, check=False, timeout=15)
    return proc.returncode == 0


def supports_tar_zstd(serial):
    proc = adb_shell(serial, "tar --help 2>&1 | grep -q -- '--zstd'", root=True, check=False, timeout=15)
    return proc.returncode == 0


def _safe_member_name(name, expected_top=None):
    if not name or "\x00" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    p = PurePosixPath(name)
    if any(part in ("", "..") for part in p.parts):
        return False
    if expected_top and p.parts and p.parts[0] != expected_top:
        return False
    return True


def validate_local_tar_archive(path, expected_top=None):
    """Validate archive structure and traversal safety before any destructive restore step."""
    if not os.path.isfile(path) or os.path.getsize(path) < 64:
        return False, "archive missing or empty"
    lower = path.lower()
    logical_lower = lower[:-5] if lower.endswith(".part") else lower
    if logical_lower.endswith((".tar.gz", ".tgz")):
        try:
            count = 0
            # Stream metadata instead of getmembers(): a backup with hundreds of
            # thousands of files must not mirror the whole tar index into RAM.
            with tarfile.open(path, "r|gz") as tf:
                for m in tf:
                    count += 1
                    if not _safe_member_name(m.name, expected_top):
                        return False, f"unsafe archive member: {m.name}"
                    if m.ischr() or m.isblk() or m.isfifo():
                        return False, f"unsupported special file: {m.name}"
                    if (m.issym() or m.islnk()):
                        # اجازه به سیم‌لینک‌های معتبر سیستمی اندروید مانند فولدر lib به محل نصب apk
                        is_safe_android_lib = (
                            m.name.endswith("/lib") 
                            and m.linkname.startswith(("/data/app/", "/system/", "/vendor/", "/apex/"))
                        )
                        if not is_safe_android_lib and not _safe_member_name(m.linkname):
                            return False, f"unsafe link target: {m.linkname}"
            return (True, "ok") if count else (False, "archive has no members")
        except Exception as exc:
            return False, f"archive validation failed: {exc}"

    if logical_lower.endswith((".tar.zst", ".tzst")):
        # Preferred path: decompress with zstd and let Python's streaming tar
        # parser inspect real TarInfo objects (including link targets) without
        # building the complete member list in memory.
        if ZSTD_PATH:
            proc = None
            stderr_buf = deque(maxlen=32)
            try:
                proc = subprocess.Popen(
                    [ZSTD_PATH, "-q", "-d", "-c", path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                )
                threading.Thread(
                    target=_bounded_reader,
                    args=(proc.stderr, queue.Queue(maxsize=1), stderr_buf),
                    daemon=True,
                ).start()
                count = 0
                with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                    for m in tf:
                        count += 1
                        if not _safe_member_name(m.name, expected_top):
                            _terminate_process(proc)
                            return False, f"unsafe archive member: {m.name}"
                        if m.ischr() or m.isblk() or m.isfifo():
                            _terminate_process(proc)
                            return False, f"unsupported special file: {m.name}"
                        if (m.issym() or m.islnk()) and not _safe_member_name(m.linkname):
                            _terminate_process(proc)
                            return False, f"unsafe link target: {m.linkname}"
                rc = proc.wait(timeout=15)
                if rc != 0:
                    err = b"".join(stderr_buf).decode("utf-8", errors="replace")[-1000:]
                    return False, f"zstd decompression failed: {err}"
                return (True, "ok") if count else (False, "archive has no members")
            except Exception as exc:
                if proc is not None:
                    _terminate_process(proc)
                return False, f"zstd archive validation failed: {exc}"

        # Security rule: a plain `tar -tf` listing does not expose enough
        # metadata to validate symlink/hardlink targets or special-file types.
        # Because restore extraction runs as root, fail closed unless we can
        # inspect real TarInfo objects through a trusted zstd decompressor.
        return False, "PC zstd decompressor is required for full safe validation of zstd archives"
    return False, "unsupported archive format"


def local_can_validate_zstd():
    # Full root-restore validation needs real TarInfo metadata. A name-only
    # `tar -tf` fallback is intentionally not considered safe enough.
    return bool(ZSTD_PATH)


def local_zstd_available():
    return bool(ZSTD_PATH)


def stream_tar_pull(serial, remote_base, folder_name, local_dest_tar, use_zstd=False,
                    speed_cb=None, should_cancel=lambda: False, idle_timeout=STREAM_IDLE_TIMEOUT,
                    compress_on_pc=False, exclude_members=None):
    """Stream a root tar to PC using a .part file and commit only after validation."""
    comp_flag = "--zstd" if use_zstd and not compress_on_pc else ("" if compress_on_pc else "-z")
    excludes = " ".join(
        f"--exclude={shlex.quote(str(member))}" for member in (exclude_members or []) if member
    )
    remote = f"tar -c {comp_flag} {excludes} -f - -C {shlex.quote(remote_base)} {shlex.quote(folder_name)}"
    remote_cmd = "su -c " + shlex.quote(remote)
    cmd = [ADB_PATH, "-s", serial, "exec-out", remote_cmd]
    part_path = local_dest_tar + ".part"
    try:
        if os.path.exists(part_path):
            os.remove(part_path)
    except OSError:
        pass

    if compress_on_pc:
        if not use_zstd or not ZSTD_PATH:
            raise AdbError("PC-side zstd compression requested but zstd is unavailable.")
        adb_proc = None
        zstd_proc = None
        adb_err = deque(maxlen=32)
        zstd_err = deque(maxlen=32)
        start = last_activity = time.monotonic()
        last_size = 0
        last_update = start
        try:
            with open(part_path, "wb") as out:
                adb_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=POPEN_FLAGS)
                _register_process(adb_proc, serial)
                zstd_proc = subprocess.Popen(
                    [ZSTD_PATH, "-q", "-T0", "-3", "-c"],
                    stdin=adb_proc.stdout, stdout=out, stderr=subprocess.PIPE, bufsize=0,
                )
                _register_process(zstd_proc, serial)
                # Let zstd own the read end. The parent must close its duplicate.
                adb_proc.stdout.close()
                threading.Thread(target=_bounded_reader, args=(adb_proc.stderr, queue.Queue(maxsize=1), adb_err), daemon=True).start()
                threading.Thread(target=_bounded_reader, args=(zstd_proc.stderr, queue.Queue(maxsize=1), zstd_err), daemon=True).start()
                while zstd_proc.poll() is None:
                    if should_cancel():
                        _terminate_process(zstd_proc); _terminate_process(adb_proc)
                        raise OperationCancelled("Backup stream cancelled.")
                    now = time.monotonic()
                    try:
                        size = os.path.getsize(part_path)
                    except OSError:
                        size = 0
                    if size != last_size:
                        last_size = size
                        last_activity = now
                        if speed_cb and now - last_update >= 0.3:
                            speed_cb(size, (size / (1024 * 1024)) / max(now - start, 0.001))
                            last_update = now
                    if adb_proc.poll() is not None and adb_proc.returncode not in (None, 0):
                        _terminate_process(zstd_proc)
                        break
                    if idle_timeout and now - last_activity > idle_timeout:
                        _terminate_process(zstd_proc); _terminate_process(adb_proc)
                        raise AdbError(f"Backup stream stalled for more than {idle_timeout}s.")
                    if now - start > 1 and shutil.disk_usage(os.path.dirname(part_path) or ".").free < 512 * 1024 * 1024:
                        _terminate_process(zstd_proc); _terminate_process(adb_proc)
                        raise AdbError("PC destination reached the 512 MB free-space safety reserve.")
                    time.sleep(0.2)
                zrc = zstd_proc.wait(timeout=10)
                arc = adb_proc.wait(timeout=10)
                out.flush(); os.fsync(out.fileno())
            if arc != 0 or zrc != 0:
                aerr = b"".join(adb_err).decode("utf-8", errors="replace")[-2000:]
                zerr = b"".join(zstd_err).decode("utf-8", errors="replace")[-2000:]
                raise AdbError(f"PC-zstd backup stream failed (adb={arc}, zstd={zrc}): {aerr} {zerr}".strip())
            ok, reason = validate_local_tar_archive(part_path, expected_top=folder_name)
            if not ok:
                raise AdbError(f"Archive integrity check failed: {reason}")
            os.replace(part_path, local_dest_tar)
            return True
        except Exception:
            if zstd_proc is not None:
                _terminate_process(zstd_proc)
            if adb_proc is not None:
                _terminate_process(adb_proc)
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass
            raise
        finally:
            if zstd_proc is not None:
                _unregister_process(zstd_proc)
            if adb_proc is not None:
                _unregister_process(adb_proc)

    q = queue.Queue(maxsize=8)
    stderr_buf = deque(maxlen=32)
    proc = None
    start = time.monotonic()
    last_data = start
    total_bytes = 0
    last_update = start
    last_space_check = start
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        _register_process(proc, serial)

        def read_stdout():
            try:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    q.put(chunk)
            finally:
                q.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=_bounded_reader, args=(proc.stderr, queue.Queue(maxsize=1), stderr_buf), daemon=True).start()

        with open(part_path, "wb") as out:
            while True:
                if should_cancel():
                    _terminate_process(proc)
                    raise OperationCancelled("Backup stream cancelled.")
                try:
                    chunk = q.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None:
                        continue
                    if idle_timeout and time.monotonic() - last_data > idle_timeout:
                        _terminate_process(proc)
                        raise AdbError(f"Backup stream stalled for more than {idle_timeout}s.")
                    continue
                if chunk is None:
                    break
                out.write(chunk)
                total_bytes += len(chunk)
                last_data = time.monotonic()
                now = last_data
                if now - last_space_check >= 1.0:
                    last_space_check = now
                    base = os.path.dirname(part_path) or "."
                    if shutil.disk_usage(base).free < 512 * 1024 * 1024:
                        _terminate_process(proc)
                        raise AdbError("PC destination reached the 512 MB free-space safety reserve.")
                if speed_cb and now - last_update >= 0.3:
                    elapsed = max(now - start, 0.001)
                    speed_cb(total_bytes, (total_bytes / (1024 * 1024)) / elapsed)
                    last_update = now
            out.flush()
            os.fsync(out.fileno())

        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            raise AdbError("tar stream did not terminate cleanly.")
        if rc != 0:
            err = b"".join(stderr_buf).decode("utf-8", errors="replace")[-4000:]
            raise AdbError(f"tar stream failed with exit code {rc}: {err}")
        ok, reason = validate_local_tar_archive(part_path, expected_top=folder_name)
        if not ok:
            raise AdbError(f"Archive integrity check failed: {reason}")
        os.replace(part_path, local_dest_tar)
        return True
    except Exception:
        try:
            if os.path.exists(part_path):
                os.remove(part_path)
        except OSError:
            pass
        raise
    finally:
        if proc is not None:
            _unregister_process(proc)


def stream_local_archive_extract(serial, local_tar, remote_base, expected_top,
                                 should_cancel=lambda: False, idle_timeout=STREAM_IDLE_TIMEOUT):
    """Validate locally, then stream into tar on-device without a compressed temp copy."""
    ok, reason = validate_local_tar_archive(local_tar, expected_top=expected_top)
    if not ok:
        raise AdbError(f"Refusing unsafe/corrupt archive: {reason}")
    is_zstd = local_tar.lower().endswith((".zst", ".tzst"))
    device_zstd = is_zstd and supports_tar_zstd(serial)
    pc_decompress = is_zstd and not device_zstd
    if pc_decompress and not ZSTD_PATH:
        raise AdbError("This device cannot extract zstd and no PC zstd decompressor is available.")

    if device_zstd:
        tar_cmd = "tar --zstd -xf -"
    elif local_tar.lower().endswith((".tar.gz", ".tgz")):
        tar_cmd = "tar -zxf -"
    else:
        tar_cmd = "tar -xf -"
    remote = f"{tar_cmd} -C {shlex.quote(remote_base)}"
    remote_cmd = "su -c " + shlex.quote(remote)
    args = [ADB_PATH, "-s", serial, "shell", remote_cmd]

    def writer(stdin, activity):
        source = None
        zproc = None
        zerr = deque(maxlen=32)
        try:
            if pc_decompress:
                zproc = subprocess.Popen(
                    [ZSTD_PATH, "-q", "-d", "-c", local_tar],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                )
                _register_process(zproc, serial)
                threading.Thread(
                    target=_bounded_reader,
                    args=(zproc.stderr, queue.Queue(maxsize=1), zerr),
                    daemon=True,
                ).start()
                source = zproc.stdout
            else:
                source = open(local_tar, "rb")

            while True:
                if should_cancel():
                    if zproc is not None:
                        _terminate_process(zproc)
                    raise OperationCancelled("Restore stream cancelled.")
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                stdin.write(chunk)
                stdin.flush()
                try:
                    activity.put_nowait(("activity", len(chunk)))
                except queue.Full:
                    pass
            if zproc is not None:
                rc = zproc.wait(timeout=15)
                if rc != 0:
                    err = b"".join(zerr).decode("utf-8", errors="replace")[-1000:]
                    raise AdbError(f"PC zstd decompression failed: {err}")
        finally:
            if source is not None and zproc is None:
                try:
                    source.close()
                except Exception:
                    pass
            if zproc is not None:
                _terminate_process(zproc)
                _unregister_process(zproc)

    proc = _run_process_cancellable(args, serial=serial, should_cancel=should_cancel,
                                    idle_timeout=idle_timeout, stdin_writer=writer)
    if proc.returncode != 0:
        raise AdbError(f"Device tar extraction failed: {(proc.stderr or proc.stdout).strip()[:1000]}")
    return True


def stream_file_to_remote(serial, local_file, remote_file, root=True,
                          should_cancel=lambda: False, idle_timeout=STREAM_IDLE_TIMEOUT):
    remote = f"cat > {shlex.quote(remote_file)}"
    remote_cmd = ("su -c " + shlex.quote(remote)) if root else remote
    args = [ADB_PATH, "-s", serial, "shell", remote_cmd]

    def writer(stdin, activity):
        with open(local_file, "rb") as src:
            while True:
                if should_cancel():
                    raise OperationCancelled("File stream cancelled.")
                chunk = src.read(64 * 1024)
                if not chunk:
                    break
                stdin.write(chunk)
                stdin.flush()
                try:
                    activity.put_nowait(("activity", len(chunk)))
                except queue.Full:
                    pass

    proc = _run_process_cancellable(args, serial=serial, should_cancel=should_cancel,
                                    idle_timeout=idle_timeout, stdin_writer=writer)
    if proc.returncode != 0:
        raise AdbError(f"Failed to stream file to device: {(proc.stderr or proc.stdout).strip()[:1000]}")
    return True


def pull_cancellable(serial, remote_path, local_path, should_cancel=lambda: False,
                     idle_timeout=TRANSFER_IDLE_TIMEOUT, pc_reserve_bytes=512 * 1024 * 1024):
    args = [ADB_PATH, "-s", serial, "pull", remote_path, local_path]
    base = local_path if os.path.isdir(local_path) else (os.path.dirname(local_path) or ".")

    def safety_check():
        try:
            if shutil.disk_usage(base).free < pc_reserve_bytes:
                return "PC destination reached the free-space safety reserve; transfer aborted before filling the disk."
        except Exception:
            pass
        return None

    return _run_process_cancellable(args, serial=serial, should_cancel=should_cancel,
                                    idle_timeout=idle_timeout, safety_check=safety_check)


def push_cancellable(serial, local_path, remote_path, should_cancel=lambda: False,
                     idle_timeout=TRANSFER_IDLE_TIMEOUT):
    args = [ADB_PATH, "-s", serial, "push", local_path, remote_path]
    return _run_process_cancellable(args, serial=serial, should_cancel=should_cancel,
                                    idle_timeout=idle_timeout)


def stream_root_command_to_file(serial, root_command, local_output_path,
                                should_cancel=lambda: False, idle_timeout=STREAM_IDLE_TIMEOUT,
                                min_size=1):
    part = local_output_path + ".part"
    try:
        if os.path.exists(part):
            os.remove(part)
    except OSError:
        pass
    cmd = [ADB_PATH, "-s", serial, "exec-out", "su -c " + shlex.quote(root_command)]
    q = queue.Queue(maxsize=8)
    stderr_buf = deque(maxlen=32)
    proc = None
    last_data = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, creationflags=POPEN_FLAGS)
        _register_process(proc, serial)

        def reader():
            try:
                while True:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    q.put(chunk)
            finally:
                q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=_bounded_reader, args=(proc.stderr, queue.Queue(maxsize=1), stderr_buf), daemon=True).start()
        with open(part, "wb") as out:
            while True:
                if should_cancel():
                    _terminate_process(proc)
                    raise OperationCancelled("Operation cancelled.")
                try:
                    chunk = q.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is None and time.monotonic() - last_data > idle_timeout:
                        _terminate_process(proc)
                        raise AdbError("ADB stream stalled.")
                    continue
                if chunk is None:
                    break
                out.write(chunk)
                last_data = time.monotonic()
                if shutil.disk_usage(os.path.dirname(part) or ".").free < 512 * 1024 * 1024:
                    _terminate_process(proc)
                    raise AdbError("PC destination reached the 512 MB free-space safety reserve.")
            out.flush()
            os.fsync(out.fileno())
        rc = proc.wait(timeout=10)
        if rc != 0 or not os.path.exists(part) or os.path.getsize(part) < min_size:
            err = b"".join(stderr_buf).decode("utf-8", errors="replace")
            raise AdbError(f"Stream command failed: {err[-2000:]}")
        os.replace(part, local_output_path)
        return True
    except Exception:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        raise
    finally:
        if proc is not None:
            _unregister_process(proc)


def dump_boot_partition(serial, local_output_path, should_cancel=lambda: False):
    proc = adb_shell(serial,
                     "ls -l /dev/block/by-name/boot /dev/block/bootdevice/by-name/boot 2>/dev/null",
                     root=True, check=False, timeout=20)
    boot_block = None
    for line in (proc.stdout or "").splitlines():
        if "->" in line:
            target = line.split("->")[-1].strip()
            if not target.startswith("/"):
                target = os.path.normpath("/dev/block/by-name/" + target).replace("\\", "/")
            boot_block = target
            break
        if "/dev/block/" in line:
            boot_block = line.strip().split()[-1]
            break
    if not boot_block:
        boot_block = "/dev/block/by-name/boot"
    return stream_root_command_to_file(serial, f"dd if={shlex.quote(boot_block)} bs=1048576 2>/dev/null",
                                       local_output_path, should_cancel=should_cancel,
                                       min_size=1024 * 1024)


def backup_root_modules(serial, local_tar_path, should_cancel=lambda: False):
    return stream_tar_pull(serial, "/data/adb", "modules", local_tar_path,
                           use_zstd=False, should_cancel=should_cancel)


def launch_scrcpy(serial, custom_path=None):
    scrcpy_exe = _FOUND_SCRCPY
    if custom_path and os.path.isfile(custom_path):
        scrcpy_exe = os.path.abspath(custom_path)
    if not scrcpy_exe:
        scrcpy_exe = shutil.which("scrcpy") or "scrcpy"
    exe_dir = os.path.dirname(scrcpy_exe) if os.path.isabs(scrcpy_exe) and os.path.isfile(scrcpy_exe) else None
    env = os.environ.copy()
    env["ADB"] = ADB_PATH
    if os.path.isabs(ADB_PATH) and os.path.isfile(ADB_PATH):
        adb_dir = os.path.dirname(os.path.abspath(ADB_PATH))
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")
    try:
        subprocess.Popen(
            [scrcpy_exe, "-s", serial, "--window-title", f"DroidVault Mirror ({serial})"],
            cwd=exe_dir, env=env, creationflags=POPEN_FLAGS
        )
        return True, "Scrcpy started successfully."
    except FileNotFoundError:
        return False, "NOT_FOUND"
    except Exception as exc:
        return False, f"Failed to start scrcpy: {exc}"


def _pm_with_root_fallback(serial, command, timeout):
    proc = adb_shell(serial, command, root=False, check=False, timeout=timeout)
    if proc.returncode == 0:
        return proc
    try:
        root_proc = adb_shell(serial, command, root=True, check=False, timeout=timeout)
        if root_proc.returncode == 0:
            return root_proc
    except Exception:
        pass
    return proc


def freeze_package(serial, package):
    proc = _pm_with_root_fallback(
        serial, f"pm disable-user --user 0 {shlex.quote(package)}", 20
    )
    return proc.returncode == 0


def unfreeze_package(serial, package):
    proc = _pm_with_root_fallback(serial, f"pm enable {shlex.quote(package)}", 20)
    return proc.returncode == 0


def uninstall_package_user0(serial, package, keep_data=True):
    keep = "-k " if keep_data else ""
    proc = _pm_with_root_fallback(
        serial, f"pm uninstall {keep}--user 0 {shlex.quote(package)}", 60
    )
    return proc.returncode == 0


def restore_system_package(serial, package):
    proc = _pm_with_root_fallback(
        serial, f"cmd package install-existing {shlex.quote(package)}", 60
    )
    return proc.returncode == 0


def get_apk_paths(serial, package):
    proc = adb_shell(serial, f"pm path {shlex.quote(package)}", check=False, timeout=20)
    return [line[8:].strip() for line in (proc.stdout or "").splitlines() if line.startswith("package:")]


def get_package_version(serial, package):
    proc = adb_shell(serial, f"dumpsys package {shlex.quote(package)}", check=False, timeout=30)
    v_name, v_code = "", ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        m1 = re.match(r"versionName=(.*)", line)
        if m1:
            v_name = m1.group(1).strip()
        m2 = re.match(r"versionCode=(\S+)", line)
        if m2:
            v_code = m2.group(1).strip()
    return {"versionName": v_name, "versionCode": v_code}


def get_package_uid(serial, package):
    proc = adb_shell(serial, f"pm list packages -U {shlex.quote(package)}", check=False, timeout=20)
    for line in (proc.stdout or "").splitlines():
        if line.startswith(f"package:{package}"):
            m = re.search(r"uid:(\d+)", line)
            if m:
                return int(m.group(1))
    proc = adb_shell(serial, f"dumpsys package {shlex.quote(package)}", check=False, timeout=30)
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("userId=") or line.startswith("appId="):
            val = line.split("=", 1)[1].strip()
            if val.isdigit():
                return int(val)
    return get_uid(serial, f"/data/data/{package}", root=True)


def pull(serial, remote_path, local_path, timeout=None):
    if timeout is None:
        timeout = 1800
    return adb(serial, ["pull", remote_path, local_path], timeout=timeout, check=False)


def push(serial, local_path, remote_path, timeout=None):
    if timeout is None:
        timeout = 1800
    return adb(serial, ["push", local_path, remote_path], timeout=timeout, check=False)


def install_multiple(serial, apk_paths, reinstall=True, downgrade=False, grant=False, timeout=None):
    args = ["install-multiple"]
    if reinstall:
        args.append("-r")
    if downgrade:
        args.append("-d")
    if grant:
        args.append("-g")
    return adb(serial, args + apk_paths, timeout=timeout or INSTALL_TIMEOUT, check=False)


def install_single(serial, apk_path, reinstall=True, downgrade=False, grant=False, timeout=None):
    args = ["install"]
    if reinstall:
        args.append("-r")
    if downgrade:
        args.append("-d")
    if grant:
        args.append("-g")
    return adb(serial, args + [apk_path], timeout=timeout or INSTALL_TIMEOUT, check=False)


def get_uid(serial, remote_path, root=True):
    proc = adb_shell(serial, f"stat -c %u {shlex.quote(remote_path)}", root=root, check=False, timeout=15)
    out = (proc.stdout or "").strip()
    return int(out) if out.isdigit() else None


def force_stop(serial, package):
    cmd = f"am force-stop {shlex.quote(package)}"
    proc = adb_shell(serial, cmd, root=False, check=False, timeout=15)
    if proc.returncode != 0:
        try:
            root_proc = adb_shell(serial, cmd, root=True, check=False, timeout=15)
            if root_proc.returncode == 0:
                return root_proc
        except Exception:
            pass
    return proc


def is_package_installed(serial, package, user_id=0):
    # `pm path` only proves package code exists on the device; packages removed
    # for user 0 can still have system APK files. Query the user's installed set.
    proc = adb_shell(
        serial, f"pm list packages --user {int(user_id)} {shlex.quote(package)}",
        check=False, timeout=20,
    )
    wanted = f"package:{package}"
    return proc.returncode == 0 and any(
        line.strip().split()[0] == wanted for line in (proc.stdout or "").splitlines()
        if line.strip().startswith("package:")
    )
