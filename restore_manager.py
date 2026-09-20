import json
import os
import re
import shlex
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import adb_utils as adbu

REMOTE_JOURNAL_ROOT = "/data/local/tmp/droidvault"
LOCAL_APK_RECOVERY_ROOT = os.path.join(adbu.CACHE_DIR, "restore_recovery")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$")


@dataclass
class RestoreResult:
    succeeded: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    unrestored: list = field(default_factory=list)
    rolled_back: list = field(default_factory=list)
    cancelled: bool = False

    def merge(self, other):
        if not other:
            return self
        self.succeeded.extend(other.succeeded)
        self.failed.extend(other.failed)
        self.skipped.extend(other.skipped)
        self.unrestored.extend(getattr(other, "unrestored", []))
        self.rolled_back.extend(other.rolled_back)
        self.cancelled = self.cancelled or other.cancelled
        return self

    @property
    def ok(self):
        return not self.failed and not self.unrestored and not self.cancelled

    @property
    def status(self):
        if self.cancelled or self.failed:
            return "failed"
        if self.unrestored:
            return "partial"
        return "complete"

    def summary(self):
        return {
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "unrestored": len(self.unrestored),
            "rolled_back": len(self.rolled_back),
            "status": self.status,
            "cancelled": self.cancelled,
        }


def list_sessions(dest_root):
    sessions = []
    if not os.path.isdir(dest_root):
        return sessions
    for name in sorted(os.listdir(dest_root), reverse=True):
        if name.endswith(".inprogress"):
            continue
        session_dir = os.path.join(dest_root, name)
        manifest_path = os.path.join(session_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            continue
        # Legacy sessions had no status field; keep them usable.
        status = manifest.get("status")
        if status is not None and status != "complete":
            continue
        sessions.append({
            "name": name, "path": session_dir, "manifest": manifest,
            "legacy_unverified": status is None or int(manifest.get("schema_version", 0) or 0) < 3,
        })
    return sessions


def _valid_package(pkg):
    return bool(PACKAGE_RE.match(str(pkg or "")))


def _safe_serial_token(serial):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(serial or "unknown"))[:120]


def _atomic_write_local_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _persist_apk_txn(txn, state=None, group_id=None):
    if state is not None:
        txn["state"] = state
    if group_id is not None:
        txn["group_id"] = group_id
    payload = {k: v for k, v in txn.items() if not k.startswith("_")}
    _atomic_write_local_json(txn["_journal"], payload)


def _cleanup_apk_txn(txn):
    path = txn.get("_dir") if isinstance(txn, dict) else None
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _create_apk_txn(serial, package, should_cancel=lambda: False):
    """Persist the current APK state on the PC before changing package code."""
    if not _valid_package(package):
        raise RuntimeError(f"Invalid package name for APK transaction: {package}")
    was_installed = adbu.is_package_installed(serial, package)
    txid = uuid.uuid4().hex[:14]
    txdir = os.path.join(LOCAL_APK_RECOVERY_ROOT, _safe_serial_token(serial), txid)
    apk_dir = os.path.join(txdir, "old_apks")
    os.makedirs(apk_dir, exist_ok=False)
    old_apks = []
    try:
        if was_installed:
            paths = adbu.get_apk_paths(serial, package)
            if not paths:
                raise RuntimeError(f"Cannot safely snapshot the currently installed APK set for {package}.")
            for index, remote in enumerate(paths):
                if should_cancel():
                    raise adbu.OperationCancelled("Restore cancelled while snapshotting current APKs.")
                base = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(remote) or f"apk_{index}.apk")
                dest = os.path.join(apk_dir, f"{index:03d}_{base}")
                part = dest + ".part"
                proc = adbu.pull_cancellable(serial, remote, part, should_cancel=should_cancel)
                if proc.returncode != 0 or not os.path.isfile(part) or os.path.getsize(part) <= 0:
                    raise RuntimeError(f"Could not snapshot current APK {remote}: {(proc.stderr or proc.stdout or '').strip()[:400]}")
                os.replace(part, dest)
                old_apks.append(dest)
        txn = {
            "schema": 1,
            "serial": serial,
            "package": package,
            "was_installed": was_installed,
            "state": "SNAPSHOT_READY",
            "group_id": "",
            "old_apks": [os.path.relpath(x, txdir) for x in old_apks],
            "_dir": txdir,
            "_journal": os.path.join(txdir, "transaction.json"),
        }
        _persist_apk_txn(txn)
        return txn
    except Exception:
        shutil.rmtree(txdir, ignore_errors=True)
        raise


def _txn_old_apks(txn):
    root = txn.get("_dir", "")
    paths = []
    for rel in txn.get("old_apks", []) or []:
        if os.path.isabs(rel) or ".." in str(rel).replace("\\", "/").split("/"):
            continue
        path = os.path.normpath(os.path.join(root, rel))
        if root and os.path.commonpath([os.path.abspath(root), os.path.abspath(path)]) == os.path.abspath(root):
            paths.append(path)
    return paths


def recover_local_apk_transactions(serial, log=lambda m: None):
    """Recover APK changes that survived a PC/app crash before package-level commit."""
    serial_root = os.path.join(LOCAL_APK_RECOVERY_ROOT, _safe_serial_token(serial))
    if not os.path.isdir(serial_root):
        return []
    recovered = []
    for name in list(os.listdir(serial_root)):
        txdir = os.path.join(serial_root, name)
        journal = os.path.join(txdir, "transaction.json")
        if not os.path.isfile(journal):
            continue
        try:
            with open(journal, "r", encoding="utf-8") as f:
                txn = json.load(f)
            txn["_dir"] = txdir
            txn["_journal"] = journal
            if str(txn.get("serial")) != str(serial) or not _valid_package(txn.get("package")):
                log(f"[Recovery] Ignoring unsafe local APK journal: {journal}")
                continue
            state = txn.get("state", "SNAPSHOT_READY")
            group_id = txn.get("group_id") or ""
            if state == "COMMITTED" or (group_id and _group_commit_exists(serial, group_id)):
                _cleanup_apk_txn(txn)
                recovered.append(txn["package"])
                log(f"[Recovery] Finalized committed APK transaction: {txn['package']}")
                continue
            if state in ("INSTALLING", "APK_CHANGED", "PENDING_GROUP"):
                ok = _restore_old_apks(
                    serial, txn["package"], _txn_old_apks(txn), bool(txn.get("was_installed")), log
                )
                if not ok:
                    log(f"[Recovery] APK rollback is still pending for {txn['package']}; local journal preserved.")
                    continue
                recovered.append(txn["package"])
                log(f"[Recovery] Rolled back interrupted APK restore: {txn['package']}")
            _cleanup_apk_txn(txn)
        except Exception as exc:
            log(f"[Recovery] Failed local APK recovery in {journal}: {exc}")
    try:
        if os.path.isdir(serial_root) and not os.listdir(serial_root):
            os.rmdir(serial_root)
    except OSError:
        pass
    return recovered


def _journal_path(opid):
    return f"{REMOTE_JOURNAL_ROOT}/{opid}.journal"


def _group_commit_path(group_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(group_id))[:80]
    return f"{REMOTE_JOURNAL_ROOT}/{safe}.commit"


def _mark_group_commit(serial, group_id):
    path = _group_commit_path(group_id)
    proc = adbu.adb_shell(
        serial,
        f"mkdir -p {shlex.quote(REMOTE_JOURNAL_ROOT)} && printf %s COMMIT > {shlex.quote(path)}",
        root=True, check=False, timeout=20,
    )
    if proc.returncode != 0:
        raise adbu.AdbError("Could not persist app-level commit marker.")


def _group_commit_exists(serial, group_id):
    if not group_id:
        return False
    return adbu.path_exists(serial, _group_commit_path(group_id), root=True)


def _remove_group_commit(serial, group_id):
    if group_id:
        adbu.adb_shell(serial, f"rm -f {shlex.quote(_group_commit_path(group_id))}",
                       root=True, check=False, timeout=15)


def _write_journal(serial, opid, data):
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    cmd = (
        f"mkdir -p {shlex.quote(REMOTE_JOURNAL_ROOT)} && chmod 700 {shlex.quote(REMOTE_JOURNAL_ROOT)} && "
        f"printf %s {shlex.quote(payload)} > {shlex.quote(_journal_path(opid))}"
    )
    proc = adbu.adb_shell(serial, cmd, root=True, check=False, timeout=20)
    if proc.returncode != 0:
        raise adbu.AdbError("Could not persist restore transaction journal.")


def _remove_journal(serial, opid):
    adbu.adb_shell(serial, f"rm -f {shlex.quote(_journal_path(opid))}", root=True, check=False, timeout=15)


def _cleanup_remote_path(serial, path):
    if not path:
        return None
    return adbu.adb_shell(serial, f"rm -rf -- {shlex.quote(path)}", root=True, check=False, timeout=30)


def _safe_transaction_paths(target, rollback):
    target = str(target or "")
    rollback = str(rollback or "")
    pkg = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+"
    allowed = (
        re.fullmatch(rf"/data/data/{pkg}(?:/databases)?", target)
        or re.fullmatch(rf"/data/(?:user|user_de)/0/{pkg}(?:/databases)?", target)
        or re.fullmatch(rf"/sdcard/Android/(?:data|obb)/{pkg}", target)
        or target in {
            "/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml",
            "/data/misc/wifi/WifiConfigStore.xml",
            "/data/misc/wifi/wpa_supplicant.conf",
        }
    )
    return bool(allowed and rollback.startswith(target + ".dv_rollback_"))


def recover_incomplete_transactions(serial, log=lambda m: None):
    """Recover interrupted restore commits before any new destructive operation."""
    recovered = []
    proc = adbu.adb_shell(
        serial,
        f"mkdir -p {shlex.quote(REMOTE_JOURNAL_ROOT)}; ls -1 {shlex.quote(REMOTE_JOURNAL_ROOT)}/*.journal 2>/dev/null || true",
        root=True, check=False, timeout=20,
    )
    for path in [p.strip() for p in (proc.stdout or "").splitlines() if p.strip()]:
        cat = adbu.adb_shell(serial, f"cat {shlex.quote(path)}", root=True, check=False, timeout=15)
        try:
            data = json.loads((cat.stdout or "").strip())
        except Exception:
            # Unknown/corrupt journal: do not guess at destructive paths.
            log(f"[Recovery] Could not parse transaction journal: {path}")
            continue
        target = data.get("target")
        rollback = data.get("rollback")
        staged = data.get("staged")
        staging_root = data.get("staging_root")
        state = data.get("state", "PREPARED")
        if not target or not rollback:
            continue
        if not _safe_transaction_paths(target, rollback):
            log(f"[Recovery] Unsafe transaction paths were ignored; journal preserved: {path}")
            continue
        try:
            resolved = True
            old_exists = bool(data.get("old_exists", adbu.path_exists(serial, rollback, root=True)))
            group_id = data.get("group_id")
            if state == "PENDING_FINALIZE" and group_id and _group_commit_exists(serial, group_id):
                # The group marker is the durable all-app commit decision. Never
                # delete the only old copy if the committed target itself vanished.
                if adbu.path_exists(serial, target, root=True):
                    _cleanup_remote_path(serial, rollback)
                    _cleanup_remote_path(serial, staged)
                    _cleanup_remote_path(serial, staging_root)
                    leftovers = [
                        p for p in (rollback, staged, staging_root)
                        if p and adbu.path_exists(serial, p, root=True)
                    ]
                    if leftovers:
                        log(f"[Recovery] Commit is valid but cleanup is still pending for {target}: {', '.join(leftovers)}")
                        continue
                    adbu.adb_shell(serial, f"rm -f {shlex.quote(path)}", root=True, check=False, timeout=15)
                    recovered.append(target)
                    log(f"[Recovery] Finalized committed restore: {target}")
                    continue
                if old_exists and rollback and adbu.path_exists(serial, rollback, root=True):
                    mv = adbu.adb_shell(
                        serial, f"mv {shlex.quote(rollback)} {shlex.quote(target)}",
                        root=True, check=False, timeout=30,
                    )
                    if mv.returncode == 0:
                        adbu.adb_shell(serial, f"rm -f {shlex.quote(path)}", root=True, check=False, timeout=15)
                        recovered.append(target)
                        log(f"[Recovery] Committed target was missing; restored previous data: {target}")
                        continue
                log(f"[Recovery] Group commit for {target} is unresolved; journal preserved.")
                continue
            if state in ("NEW_READY", "OLD_MOVED", "PENDING_FINALIZE"):
                # Anything not explicitly finalized is rolled back. This also
                # makes a crash during a multi-component app restore safe.
                if old_exists:
                    if adbu.path_exists(serial, rollback, root=True):
                        if adbu.path_exists(serial, target, root=True):
                            _cleanup_remote_path(serial, target)
                        mv = adbu.adb_shell(
                            serial, f"mv {shlex.quote(rollback)} {shlex.quote(target)}",
                            root=True, check=False, timeout=30,
                        )
                        if mv.returncode == 0:
                            recovered.append(target)
                            log(f"[Recovery] Rolled back interrupted restore: {target}")
                        else:
                            resolved = False
                            log(f"[Recovery] Rollback is still pending for {target}; journal preserved.")
                    elif not adbu.path_exists(serial, target, root=True):
                        # The journal says an original target existed, but neither
                        # the target nor its rollback copy can be found. Never
                        # discard the only recovery metadata in this state.
                        resolved = False
                        log(f"[Recovery] Original target is missing and no rollback copy exists: {target}")
                else:
                    # No target existed before this transaction. If a crash
                    # happened after staged->target but before the journal state
                    # advanced, remove the newly created target to restore the
                    # exact pre-restore state.
                    if adbu.path_exists(serial, target, root=True):
                        _cleanup_remote_path(serial, target)
                        if adbu.path_exists(serial, target, root=True):
                            resolved = False
                        else:
                            recovered.append(target)
            elif state == "COMMITTED":
                if adbu.path_exists(serial, target, root=True):
                    _cleanup_remote_path(serial, rollback)
                    if rollback and adbu.path_exists(serial, rollback, root=True):
                        resolved = False
                        log(f"[Recovery] Committed restore is intact but rollback cleanup is pending: {target}")
                elif old_exists and adbu.path_exists(serial, rollback, root=True):
                    mv = adbu.adb_shell(
                        serial, f"mv {shlex.quote(rollback)} {shlex.quote(target)}",
                        root=True, check=False, timeout=30,
                    )
                    if mv.returncode == 0:
                        recovered.append(target)
                    else:
                        resolved = False
                        log(f"[Recovery] Committed target missing and rollback could not be restored: {target}")
                elif not old_exists:
                    # The transaction created a new target, but that target vanished.
                    # There is nothing safe to reconstruct; preserve the journal so a
                    # later operator can inspect the abnormal state instead of hiding it.
                    resolved = False
                    log(f"[Recovery] Committed new target is missing: {target}; journal preserved.")
            # PREPARED / NEW_READY with no rollback means original target was never moved.
            _cleanup_remote_path(serial, staged)
            _cleanup_remote_path(serial, staging_root)
            if staged and adbu.path_exists(serial, staged, root=True):
                resolved = False
            if staging_root and adbu.path_exists(serial, staging_root, root=True):
                resolved = False
            if resolved:
                adbu.adb_shell(serial, f"rm -f {shlex.quote(path)}", root=True, check=False, timeout=15)
        except Exception as exc:
            log(f"[Recovery] Failed to recover {target}: {exc}")

    # Remove group commit markers only when no transaction journal references them.
    groups = adbu.adb_shell(
        serial, f"ls -1 {shlex.quote(REMOTE_JOURNAL_ROOT)}/*.commit 2>/dev/null || true",
        root=True, check=False, timeout=15,
    )
    for marker in [m.strip() for m in (groups.stdout or "").splitlines() if m.strip()]:
        group_id = os.path.basename(marker)[:-7]
        grep = adbu.adb_shell(
            serial,
            f"grep -l {shlex.quote(chr(34) + 'group_id' + chr(34) + ':' + chr(34) + group_id + chr(34))} "
            f"{shlex.quote(REMOTE_JOURNAL_ROOT)}/*.journal 2>/dev/null | head -n 1 || true",
            root=True, check=False, timeout=15,
        )
        if not (grep.stdout or "").strip():
            adbu.adb_shell(serial, f"rm -f {shlex.quote(marker)}", root=True, check=False, timeout=15)
    return recovered


def _verify_checksum(local_path, expected, required=False):
    if not os.path.isfile(local_path):
        return False, "file is missing"
    if not expected:
        return (not required), ("checksum missing" if required else "legacy backup without checksum")
    actual = adbu.calculate_sha256(local_path)
    if actual.lower() != str(expected).lower():
        return False, f"SHA-256 mismatch ({actual[:12]} != {str(expected)[:12]})"
    return True, "ok"


def _archive_checksum(entry, key, file_name, schema_version):
    checksums = entry.get("checksums", {}) if isinstance(entry, dict) else {}
    expected = checksums.get(file_name) or checksums.get(key)
    return expected, int(schema_version or 0) >= 3


def _preflight_archive(local_tar, expected_top, expected_sha256, checksum_required=True):
    ok, reason = _verify_checksum(local_tar, expected_sha256, required=checksum_required)
    if not ok:
        raise RuntimeError(reason)
    ok, reason = adbu.validate_local_tar_archive(local_tar, expected_top=expected_top)
    if not ok:
        raise RuntimeError(reason)


def _preflight_free_space(serial, remote_base_dir, expected_uncompressed, local_tar):
    free_mb = adbu.get_free_space_mb(serial, remote_base_dir)
    if free_mb <= 0:
        raise RuntimeError(f"Could not determine free space for {remote_base_dir}; refusing fail-open restore.")
    if expected_uncompressed and expected_uncompressed > 0:
        required = int(expected_uncompressed * 1.15) + 64 * 1024 * 1024
    else:
        # Legacy conservative estimate. This is only used when old manifests lack uncompressed size.
        required = max(os.path.getsize(local_tar) * 4, 256 * 1024 * 1024)
    if free_mb * 1024 * 1024 < required:
        raise RuntimeError(
            f"Insufficient free space on {remote_base_dir}: {free_mb} MB free, "
            f"approximately {required // (1024 * 1024)} MB required."
        )


def _rollback_commit(serial, handle, log=lambda m: None):
    """Rollback one committed-but-not-finalized component. Safe to call repeatedly."""
    if not handle:
        return True
    target = handle.get("target")
    rollback = handle.get("rollback")
    staging_root = handle.get("staging_root")
    staged = handle.get("staged")
    opid = handle.get("opid")
    old_exists = bool(handle.get("old_exists"))
    resolved = True
    try:
        if old_exists:
            if rollback and adbu.path_exists(serial, rollback, root=True):
                if target and adbu.path_exists(serial, target, root=True):
                    _cleanup_remote_path(serial, target)
                mv = adbu.adb_shell(
                    serial, f"mv {shlex.quote(rollback)} {shlex.quote(target)}",
                    root=True, check=False, timeout=45,
                )
                if mv.returncode != 0:
                    resolved = False
                else:
                    log(f"  Rolled back: {target}")
            else:
                # A pending transaction that originally had data is only truly
                # rollback-capable while its rollback copy still exists.
                resolved = False
        else:
            # There was no original target. A failed transaction must remove the
            # newly committed target rather than leave a partial logical restore.
            if target and adbu.path_exists(serial, target, root=True):
                _cleanup_remote_path(serial, target)
                if adbu.path_exists(serial, target, root=True):
                    resolved = False
        _cleanup_remote_path(serial, staged)
        _cleanup_remote_path(serial, staging_root)
        if staged and adbu.path_exists(serial, staged, root=True):
            resolved = False
        if staging_root and adbu.path_exists(serial, staging_root, root=True):
            resolved = False
    finally:
        if resolved and opid:
            _remove_journal(serial, opid)
    return resolved


def _finalize_commit(serial, handle):
    """Finalize a committed component; leave its journal if cleanup must be retried."""
    if not handle:
        return True
    opid = handle.get("opid")
    group_id = handle.get("group_id")
    if not group_id:
        journal = dict(handle)
        journal.pop("opid", None)
        journal["state"] = "COMMITTED"
        if opid:
            _write_journal(serial, opid, journal)

    cleanup_ok = True
    rollback = handle.get("rollback")
    if rollback:
        _cleanup_remote_path(serial, rollback)
        if adbu.path_exists(serial, rollback, root=True):
            cleanup_ok = False
    staging_root = handle.get("staging_root")
    _cleanup_remote_path(serial, staging_root)
    if staging_root and adbu.path_exists(serial, staging_root, root=True):
        cleanup_ok = False
    if cleanup_ok and opid:
        _remove_journal(serial, opid)
    return cleanup_ok


def _commit_staged_path(serial, target, staged, staging_root, opid, log,
                        post_commit_cmd=None, defer_finalize=False):
    rollback = f"{target}.dv_rollback_{opid}"
    old_exists = adbu.path_exists(serial, target, root=True)
    journal = {
        "state": "NEW_READY",
        "target": target,
        "rollback": rollback,
        "staged": staged,
        "staging_root": staging_root,
        "old_exists": old_exists,
    }
    _write_journal(serial, opid, journal)
    try:
        if old_exists:
            _cleanup_remote_path(serial, rollback)
            proc = adbu.adb_shell(
                serial, f"mv {shlex.quote(target)} {shlex.quote(rollback)}",
                root=True, check=False, timeout=45,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Could not move current target to rollback: {(proc.stderr or proc.stdout).strip()[:300]}")
        journal["state"] = "OLD_MOVED"
        _write_journal(serial, opid, journal)

        proc = adbu.adb_shell(
            serial, f"mv {shlex.quote(staged)} {shlex.quote(target)}",
            root=True, check=False, timeout=45,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Could not commit staged restore: {(proc.stderr or proc.stdout).strip()[:300]}")

        if post_commit_cmd:
            post = adbu.adb_shell(serial, post_commit_cmd, root=True, check=False, timeout=120)
            if post.returncode != 0:
                raise RuntimeError(f"Post-commit verification/context step failed: {(post.stderr or post.stdout).strip()[:300]}")

        handle = dict(journal)
        handle.update({"state": "PENDING_FINALIZE", "opid": opid})
        _write_journal(serial, opid, {k: v for k, v in handle.items() if k != "opid"})
        # The staged directory has been moved out, so the namespace itself can be
        # removed without affecting the rollback copy.
        _cleanup_remote_path(serial, staging_root)
        if defer_finalize:
            return handle
        _finalize_commit(serial, handle)
        return True
    except Exception:
        handle = dict(journal)
        handle["opid"] = opid
        if not _rollback_commit(serial, handle, log):
            log(f"  [Critical] Automatic rollback is pending for {target}; recovery journal preserved.")
        raise

def _restore_data_tar(serial, target_package, archive_top, local_tar, remote_base_dir,
                      chown_and_context, log, should_cancel=lambda: False,
                      expected_sha256=None, checksum_required=False, expected_uncompressed=0,
                      defer_finalize=False):
    if target_package and not _valid_package(target_package):
        raise RuntimeError(f"Invalid target package name: {target_package}")
    _preflight_archive(local_tar, archive_top, expected_sha256, checksum_required)
    _preflight_free_space(serial, remote_base_dir, expected_uncompressed, local_tar)
    if should_cancel():
        raise adbu.OperationCancelled("Restore cancelled.")

    opid = uuid.uuid4().hex[:10]
    staging_root = f"{remote_base_dir}/.droidvault_stage_{opid}"
    staged = f"{staging_root}/{archive_top}"
    target = f"{remote_base_dir}/{archive_top}"
    prep = adbu.adb_shell(
        serial, f"rm -rf {shlex.quote(staging_root)} && mkdir -p {shlex.quote(staging_root)}",
        root=True, check=False, timeout=30,
    )
    if prep.returncode != 0:
        raise RuntimeError(f"Could not prepare staging directory: {(prep.stderr or prep.stdout).strip()[:300]}")

    journal = {
        "state": "PREPARED", "target": target,
        "rollback": f"{target}.dv_rollback_{opid}", "staged": staged, "staging_root": staging_root,
    }
    try:
        _write_journal(serial, opid, journal)
        adbu.stream_local_archive_extract(
            serial, local_tar, staging_root, archive_top,
            should_cancel=should_cancel,
        )
        if not adbu.path_exists(serial, staged, root=True):
            raise RuntimeError("Archive extracted without the expected top-level directory.")

        if chown_and_context:
            uid = adbu.get_package_uid(serial, target_package)
            if uid is None:
                raise RuntimeError(f"Could not resolve UID for {target_package}.")
            proc = adbu.adb_shell(
                serial, f"chown -R {uid}:{uid} {shlex.quote(staged)}",
                root=True, check=False, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError("Could not apply ownership to staged data.")

        if expected_uncompressed and expected_uncompressed > 0:
            staged_size = adbu.get_remote_size_bytes(serial, staged, root=True)
            tolerance = max(int(expected_uncompressed * 0.08), 4 * 1024 * 1024)
            if staged_size <= 0 or abs(staged_size - int(expected_uncompressed)) > tolerance:
                raise RuntimeError(
                    f"Extracted size verification failed ({staged_size} vs expected {expected_uncompressed} bytes)."
                )

        if should_cancel():
            raise adbu.OperationCancelled("Restore cancelled before commit.")
        if target_package:
            stop_proc = adbu.force_stop(serial, target_package)
            if stop_proc.returncode != 0:
                raise RuntimeError(f"Could not force-stop {target_package} before transactional commit.")
        post_cmd = f"restorecon -R {shlex.quote(target)}" if chown_and_context else None
        return _commit_staged_path(
            serial, target, staged, staging_root, opid, log,
            post_commit_cmd=post_cmd, defer_finalize=defer_finalize,
        )
    except Exception:
        _cleanup_remote_path(serial, staging_root)
        # If commit did not move old data, journal can be removed. If it did, _commit_staged_path handled rollback.
        if not adbu.path_exists(serial, journal["rollback"], root=True):
            _remove_journal(serial, opid)
        raise


def _find_provider_base(serial, package):
    candidates = [
        f"/data/user_de/0/{package}",
        f"/data/data/{package}",
        f"/data/user/0/{package}",
    ]
    for base in candidates:
        if adbu.path_exists(serial, base, root=True):
            return base
    return None



def _database_kind(file_name, package=""):
    low = (file_name or "").lower()
    pkg = (package or "").lower()
    if "sms" in low or "telephony" in pkg:
        return "sms"
    if "call" in low or "logsprovider" in pkg:
        return "calllogs"
    return "contacts"

def _database_targets(kind):
    if kind == "sms":
        packages = [("com.android.providers.telephony", "mmssms.db")]
    elif kind == "calllogs":
        packages = [
            ("com.sec.android.provider.logsprovider", "logs.db"),
            ("com.android.providers.contacts", "calllog.db"),
        ]
    else:
        packages = [
            ("com.android.providers.contacts", "contacts2.db"),
            ("com.samsung.android.providers.contacts", "contacts2.db"),
        ]
    result = []
    for package, db_name in packages:
        for base in (f"/data/user_de/0/{package}", f"/data/data/{package}", f"/data/user/0/{package}"):
            result.append((package, base, db_name))
    return result

def _find_target_database(serial, kind):
    for package, base, db_name in _database_targets(kind):
        path = f"{base}/databases/{db_name}"
        if adbu.path_exists(serial, path, root=True):
            return package, base, db_name, path
    return None, None, None, None


def _sqlite_signature(path, required_tables=()):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {row[0] if row else 'no result'}")
        tables = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r and r[0] and not str(r[0]).startswith("sqlite_")
        }
        missing = [t for t in required_tables if t not in tables]
        if missing:
            raise RuntimeError("required SQLite tables missing: " + ", ".join(missing))
        schema = []
        for table in sorted(tables):
            cols = [str(r[1]) for r in con.execute(f'PRAGMA table_info("{table.replace(chr(34), chr(34)*2)}")')]
            schema.append((table, tuple(cols)))
        user_version = int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
        return {"tables": tables, "schema": tuple(schema), "user_version": user_version}
    finally:
        con.close()


def _inspect_database_archive(local_tar, preferred_db, kind):
    required = ("sms",) if kind == "sms" else (("contacts", "raw_contacts") if kind == "contacts" else ())
    with tarfile.open(local_tar, "r:gz") as tf:
        members = tf.getmembers()
        candidates = [m for m in members if m.isfile() and os.path.basename(m.name) == preferred_db]
        if not candidates:
            candidates = [m for m in members if m.isfile() and m.name.lower().endswith(".db")]
        if not candidates:
            raise RuntimeError("no SQLite database file found in backup archive")
        main = candidates[0]
        base_name = os.path.basename(main.name)
        wanted = {base_name, base_name + "-wal", base_name + "-shm"}
        with tempfile.TemporaryDirectory(prefix="dv_dbinspect_") as td:
            for member in members:
                bn = os.path.basename(member.name)
                if not member.isfile() or bn not in wanted:
                    continue
                src = tf.extractfile(member)
                if src is None:
                    continue
                with open(os.path.join(td, bn), "wb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
            db_path = os.path.join(td, base_name)
            if not os.path.isfile(db_path):
                raise RuntimeError("could not materialize SQLite database from archive")
            return _sqlite_signature(db_path, required), main.name


def _inspect_device_database(serial, remote_path, kind, should_cancel=lambda: False):
    required = ("sms",) if kind == "sms" else (("contacts", "raw_contacts") if kind == "contacts" else ())
    fd, tmp = tempfile.mkstemp(prefix="dv_target_db_", suffix=".db")
    os.close(fd)
    try:
        adbu.stream_root_command_to_file(
            serial, f"cat {shlex.quote(remote_path)}", tmp,
            should_cancel=should_cancel, min_size=64,
        )
        return _sqlite_signature(tmp, required)
    finally:
        try: os.remove(tmp)
        except OSError: pass


def _schemas_compatible(source_sig, target_sig):
    # Raw replacement is allowed across provider/version/path changes only when
    # table+column layout is identical. user_version may differ across vendors
    # without implying incompatibility, so it is diagnostic rather than a gate.
    return bool(source_sig and target_sig and source_sig.get("schema") == target_sig.get("schema"))


def _restore_database_component(serial, sys_dir, meta, legacy_file, legacy_package,
                                log, should_cancel=lambda: False):
    meta = meta if isinstance(meta, dict) else {}
    source_package = meta.get("source_package") or legacy_package
    file_name = meta.get("file") or legacy_file
    if os.path.basename(file_name) != file_name:
        return "failed", f"unsafe database archive filename: {file_name}"
    local_tar = os.path.join(sys_dir, file_name)
    if not os.path.isfile(local_tar):
        return "skipped", f"{file_name} not found"

    expected = meta.get("sha256") if meta else None
    expected_size = int(meta.get("uncompressed_bytes") or 0) if meta else 0
    checksum_required = bool(meta)
    ok, reason = _verify_checksum(local_tar, expected, required=checksum_required)
    if not ok:
        return "failed", reason

    kind = _database_kind(file_name, source_package)
    package, base, db_name, remote_db = _find_target_database(serial, kind)
    if not base:
        return "unrestored", f"no compatible {kind} provider/database is present on this ROM"

    try:
        source_sig, source_member = _inspect_database_archive(local_tar, db_name, kind)
        target_sig = _inspect_device_database(serial, remote_db, kind, should_cancel)
    except adbu.OperationCancelled:
        raise
    except Exception as exc:
        return "unrestored", f"database compatibility check failed: {exc}"

    if not _schemas_compatible(source_sig, target_sig):
        return "unrestored", (
            f"{kind} SQLite schema differs from target provider; raw overwrite refused "
            f"(backup provider {source_package or '?'}, target {package})"
        )

    source_version = (meta.get("source_version") or {}) if meta else {}
    source_code = str(source_version.get("versionCode") or "")
    current_code = ""
    try:
        current_code = str(adbu.get_package_version(serial, package).get("versionCode") or "")
    except Exception:
        pass
    if source_code and current_code and source_code != current_code:
        log(
            f"  [Compatibility] {kind} provider version differs ({source_code} -> {current_code}), "
            "but SQLite schema matches exactly; continuing transactionally."
        )

    try:
        _restore_data_tar(
            serial, package, "databases", local_tar, base, True, log,
            should_cancel=should_cancel,
            expected_sha256=expected, checksum_required=checksum_required,
            expected_uncompressed=expected_size,
        )
        return "ok", f"{package} ({source_member})"
    except adbu.OperationCancelled:
        raise
    except Exception as exc:
        return "failed", str(exc)


def _validate_wifi_file(local_path):
    if local_path.lower().endswith(".xml"):
        try:
            ET.parse(local_path)
        except Exception as exc:
            raise RuntimeError(f"Invalid Wi-Fi XML: {exc}")


def _resolve_wifi_target(serial, source_path, basename):
    known = [
        "/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml",
        "/data/misc/wifi/WifiConfigStore.xml",
        "/data/misc/wifi/wpa_supplicant.conf",
    ]
    if source_path in known and adbu.path_exists(serial, source_path, root=True):
        return source_path
    # Backup files are intentionally prefixed (wifi_00_...). Compare the
    # canonical config basename, not the PC cache filename.
    canonical = re.sub(r"^wifi_\d+_", "", os.path.basename(basename or ""), flags=re.I)
    if source_path in known:
        canonical = os.path.basename(source_path)
    for path in known:
        if os.path.basename(path) == canonical and adbu.path_exists(serial, path, root=True):
            return path
    return None


def _transactional_restore_file(serial, local_file, target, log, should_cancel=lambda: False):
    opid = uuid.uuid4().hex[:10]
    parent = os.path.dirname(target)
    staged = f"{parent}/.{os.path.basename(target)}.dv_new_{opid}"
    rollback = f"{target}.dv_rollback_{opid}"
    stat = adbu.adb_shell(serial, f"stat -c '%u %g %a' {shlex.quote(target)} 2>/dev/null",
                          root=True, check=False, timeout=15)
    attrs = (stat.stdout or "").strip().split()
    journal = {"state": "PREPARED", "target": target, "rollback": rollback, "staged": staged, "staging_root": ""}
    try:
        _write_journal(serial, opid, journal)
        adbu.stream_file_to_remote(serial, local_file, staged, root=True, should_cancel=should_cancel)
        if attrs and len(attrs) == 3:
            uid, gid, mode = attrs
            adbu.adb_shell(
                serial, f"chown {uid}:{gid} {shlex.quote(staged)} && chmod {mode} {shlex.quote(staged)}",
                root=True, check=True, timeout=20,
            )
        else:
            adbu.adb_shell(
                serial, f"chown wifi:wifi {shlex.quote(staged)} && chmod 600 {shlex.quote(staged)}",
                root=True, check=True, timeout=20,
            )
        _commit_staged_path(
            serial, target, staged, "", opid, log,
            post_commit_cmd=f"restorecon {shlex.quote(target)}",
        )
        return True
    except Exception:
        _cleanup_remote_path(serial, staged)
        if not adbu.path_exists(serial, rollback, root=True):
            _remove_journal(serial, opid)
        raise


def _restore_wifi(serial, sys_dir, meta, log, should_cancel=lambda: False):
    result = RestoreResult()
    files = []
    if isinstance(meta, dict) and isinstance(meta.get("files"), list):
        files = meta["files"]
    else:
        # Legacy backups. Support both historical names without pretending they are interchangeable.
        for name in ("WifiConfigStore.xml", "wpa_supplicant.conf"):
            if os.path.isfile(os.path.join(sys_dir, name)):
                files.append({"file": name, "source_path": "", "sha256": None})

    for info in files:
        if should_cancel():
            raise adbu.OperationCancelled("Restore cancelled.")
        file_name = info.get("file")
        if not file_name or os.path.basename(file_name) != file_name:
            result.failed.append(f"Wi-Fi:{file_name}:unsafe backup filename")
            continue
        local_path = os.path.join(sys_dir, file_name)
        if not os.path.isfile(local_path):
            result.skipped.append(f"Wi-Fi:{file_name}:missing")
            continue
        ok, reason = _verify_checksum(local_path, info.get("sha256"), required=bool(info.get("sha256")))
        if not ok:
            result.failed.append(f"Wi-Fi:{file_name}:{reason}")
            continue
        try:
            _validate_wifi_file(local_path)
            target = _resolve_wifi_target(serial, info.get("source_path"), os.path.basename(file_name))
            if not target:
                result.skipped.append(f"Wi-Fi:{file_name}:no compatible target")
                continue
            _transactional_restore_file(serial, local_path, target, log, should_cancel)
            result.succeeded.append(f"Wi-Fi:{target}")
            log(f"  Restored Wi-Fi config: {target}")
        except adbu.OperationCancelled:
            raise
        except Exception as exc:
            result.failed.append(f"Wi-Fi:{file_name}:{exc}")
    return result


def restore_system_personal_data(serial, session_dir, manifest, options, log=lambda m: None,
                                 should_cancel=lambda: False):
    result = RestoreResult()
    sys_dir = os.path.join(session_dir, "system_data")
    if not os.path.isdir(sys_dir):
        return result
    system_meta = manifest.get("system_data", {}) if isinstance(manifest.get("system_data"), dict) else {}

    if options.get("restore_wifi", True):
        log("Restoring Wi-Fi configurations...")
        backup_sdk = str((manifest.get("device") or {}).get("sdk") or "")
        current_sdk = str(adbu.get_device_info(serial).get("sdk") or "")
        if backup_sdk not in ("", "?") and current_sdk not in ("", "?") and backup_sdk != current_sdk:
            log(f"  [Compatibility] Android SDK differs ({backup_sdk} -> {current_sdk}); resolving Wi-Fi config by canonical format/path.")
        result.merge(_restore_wifi(serial, sys_dir, system_meta.get("wifi"), log, should_cancel))

    if options.get("restore_sms", True):
        log("Restoring SMS/MMS databases...")
        status, info = _restore_database_component(
            serial, sys_dir, system_meta.get("sms"), "sms_databases.tar.gz",
            "com.android.providers.telephony", log, should_cancel,
        )
        getattr(result, "succeeded" if status == "ok" else status).append(f"SMS:{info}")

    if options.get("restore_contacts", True):
        contacts_meta = system_meta.get("contacts_and_calls")
        if isinstance(contacts_meta, dict):
            c_meta = contacts_meta.get("contacts")
            s_meta = contacts_meta.get("samsung_calllogs")
        else:
            c_meta = system_meta.get("contacts")
            s_meta = None

        log("Restoring Contacts databases...")
        legacy_pkg = "com.android.providers.contacts"
        if isinstance(c_meta, dict) and c_meta.get("source_package"):
            legacy_pkg = c_meta["source_package"]
        status, info = _restore_database_component(
            serial, sys_dir, c_meta, "contacts_databases.tar.gz", legacy_pkg, log, should_cancel,
        )
        getattr(result, "succeeded" if status == "ok" else status).append(f"Contacts:{info}")

        if os.path.isfile(os.path.join(sys_dir, "samsung_calllogs.tar.gz")) or (isinstance(s_meta, dict) and s_meta.get("included")):
            log("Restoring Samsung Call History...")
            status, info = _restore_database_component(
                serial, sys_dir, s_meta, "samsung_calllogs.tar.gz",
                "com.sec.android.provider.logsprovider", log, should_cancel,
            )
            getattr(result, "succeeded" if status == "ok" else status).append(f"CallLogs:{info}")

    return result


def _apk_install_success(proc):
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    return proc.returncode == 0 and ("success" in text or not text.strip())


def _snapshot_current_apks(serial, package, temp_dir, should_cancel):
    paths = adbu.get_apk_paths(serial, package)
    local = []
    for index, remote in enumerate(paths):
        name = os.path.basename(remote) or f"apk_{index}.apk"
        dest = os.path.join(temp_dir, name)
        proc = adbu.pull_cancellable(serial, remote, dest, should_cancel=should_cancel)
        if proc.returncode != 0 or not os.path.isfile(dest):
            raise RuntimeError(f"Could not snapshot current APK {remote}")
        local.append(dest)
    return local


def _restore_old_apks(serial, package, old_apks, was_installed, log):
    try:
        if was_installed and old_apks:
            proc = (adbu.install_multiple(serial, old_apks, reinstall=True, downgrade=True)
                    if len(old_apks) > 1 else adbu.install_single(serial, old_apks[0], reinstall=True, downgrade=True))
            if _apk_install_success(proc):
                log(f"  Rolled back APK version: {package}")
                return True
        elif not was_installed:
            if not adbu.is_package_installed(serial, package):
                return True
            if adbu.uninstall_package_user0(serial, package, keep_data=False):
                log(f"  Removed newly installed APK after failed restore: {package}")
                return True
    except Exception:
        pass
    return False


def _preflight_app_files(session_dir, manifest, selected_packages, options):
    schema = int(manifest.get("schema_version", 0) or 0)
    apps_by_pkg = {a.get("package"): a for a in manifest.get("apps", []) if _valid_package(a.get("package"))}
    errors = {}
    for pkg in selected_packages:
        entry = apps_by_pkg.get(pkg)
        if not entry:
            errors[pkg] = "package not found in manifest"
            continue
        pkg_dir = os.path.join(session_dir, "apps", pkg)
        try:
            if options.get("restore_apk", True) and entry.get("apk_files"):
                for name in entry["apk_files"]:
                    if os.path.basename(name) != name:
                        raise RuntimeError(f"unsafe APK filename: {name}")
                    path = os.path.join(pkg_dir, "apk", name)
                    expected = entry.get("checksums", {}).get(f"apk/{name}")
                    ok, reason = _verify_checksum(path, expected, required=schema >= 3)
                    if not ok:
                        raise RuntimeError(f"{name}: {reason}")
            for key, enabled in (("data", options.get("restore_data", True)),
                                 ("extdata", options.get("restore_extdata", True)),
                                 ("obb", options.get("restore_obb", True))):
                if not enabled or not entry.get(f"has_{key}", False):
                    continue
                file_name = f"{key}.tar.zst" if os.path.isfile(os.path.join(pkg_dir, f"{key}.tar.zst")) else f"{key}.tar.gz"
                path = os.path.join(pkg_dir, file_name)
                expected = entry.get("checksums", {}).get(file_name)
                _preflight_archive(path, pkg, expected, checksum_required=schema >= 3)
        except Exception as exc:
            errors[pkg] = str(exc)
    return apps_by_pkg, errors


def restore_apps(serial, session_dir, manifest, selected_packages, options,
                 log=lambda m: None, progress=lambda i, total: None,
                 should_cancel=lambda: False):
    result = RestoreResult()
    apps_by_pkg, preflight_errors = _preflight_app_files(
        session_dir, manifest, selected_packages, options
    )
    total = len(selected_packages)
    schema = int(manifest.get("schema_version", 0) or 0)

    for idx, pkg in enumerate(selected_packages, start=1):
        if should_cancel():
            result.cancelled = True
            raise adbu.OperationCancelled("Restore cancelled.")
        if not _valid_package(pkg):
            result.failed.append(f"{pkg}: invalid package name")
            progress(idx, total)
            continue
        if pkg in preflight_errors:
            result.failed.append(f"{pkg}: preflight failed: {preflight_errors[pkg]}")
            log(f"[{idx}/{total}] Skipping {pkg}: preflight failed.")
            progress(idx, total)
            continue

        entry = apps_by_pkg[pkg]
        pkg_dir = os.path.join(session_dir, "apps", pkg)
        log(f"[{idx}/{total}] Restoring {pkg} ...")

        was_installed = adbu.is_package_installed(serial, pkg)
        will_install_apk = bool(options.get("restore_apk", True) and entry.get("apk_files"))
        has_data_restore = bool(
            (options.get("restore_data", True) and entry.get("has_data"))
            or (options.get("restore_extdata", True) and entry.get("has_extdata"))
            or (options.get("restore_obb", True) and entry.get("has_obb"))
        )

        # If package code will not be restored together with its data, require a
        # compatible installed version. This avoids applying an old/new data
        # schema to unrelated package code.
        if has_data_restore and not will_install_apk:
            if not was_installed:
                result.failed.append(f"{pkg}: app is not installed and no APK restore is selected/available")
                log(f"  [Failed] {pkg}: no installed package is available for data-only restore.")
                progress(idx, total)
                continue
            backup_code = str(entry.get("versionCode") or "")
            current_code = str(adbu.get_package_version(serial, pkg).get("versionCode") or "")
            if backup_code and current_code:
                try:
                    bcode, ccode = int(str(backup_code).split()[0]), int(str(current_code).split()[0])
                except ValueError:
                    bcode = ccode = None
                if bcode is not None and ccode is not None and ccode < bcode:
                    result.failed.append(
                        f"{pkg}: installed app is older than backup data (backup {backup_code}, device {current_code})"
                    )
                    log(f"  [Failed] {pkg}: refusing newer data on an older installed app.")
                    progress(idx, total)
                    continue
                if backup_code != current_code:
                    log(
                        f"  [Compatibility] {pkg}: data-only version differs "
                        f"(backup {backup_code}, device {current_code}); continuing transactionally."
                    )
            elif schema >= 3:
                log(f"  [Compatibility] {pkg}: version metadata incomplete; continuing transactionally with rollback protection.")

        transaction_handles = []
        apk_txn = None
        installed_backup_apk = False
        package_failed = False
        package_rolled_back = False
        commit_decided = False
        permissions_before = set()
        newly_granted_permissions = []
        permission_failures = []

        try:
            if will_install_apk:
                # Durable PC-side snapshot: survives app/PC process crashes and is
                # reconciled with the device-side group commit marker on reconnect.
                apk_txn = _create_apk_txn(serial, pkg, should_cancel)
                _persist_apk_txn(apk_txn, state="INSTALLING")
                stop_proc = adbu.force_stop(serial, pkg)
                if was_installed and stop_proc.returncode != 0:
                    raise RuntimeError(f"Could not force-stop {pkg} before APK replacement.")
                apk_paths = [os.path.join(pkg_dir, "apk", n) for n in entry["apk_files"]]
                proc = (adbu.install_multiple(serial, apk_paths, reinstall=True, downgrade=True, grant=False)
                        if len(apk_paths) > 1 else
                        adbu.install_single(serial, apk_paths[0], reinstall=True, downgrade=True, grant=False))
                if not _apk_install_success(proc):
                    raise RuntimeError(f"APK install failed: {(proc.stderr or proc.stdout).strip()[:500]}")
                installed_backup_apk = True
                _persist_apk_txn(apk_txn, state="APK_CHANGED")
                log(f"  Installed APK: {pkg}")

            if has_data_restore:
                stop_proc = adbu.force_stop(serial, pkg)
                if stop_proc.returncode != 0:
                    raise RuntimeError(f"Could not force-stop {pkg} before restoring its data.")

            data_tar = os.path.join(pkg_dir, "data.tar.zst")
            if not os.path.isfile(data_tar):
                data_tar = os.path.join(pkg_dir, "data.tar.gz")
            if options.get("restore_data", True) and entry.get("has_data") and os.path.isfile(data_tar):
                expected = entry.get("checksums", {}).get(os.path.basename(data_tar))
                handle = _restore_data_tar(
                    serial, pkg, pkg, data_tar, "/data/data", True, log,
                    should_cancel=should_cancel, expected_sha256=expected,
                    checksum_required=schema >= 3,
                    expected_uncompressed=entry.get("uncompressed_bytes", {}).get("data", 0),
                    defer_finalize=True,
                )
                transaction_handles.append(handle)
                log(f"  Restored internal data (pending app commit): {pkg}")

            ext_tar = os.path.join(pkg_dir, "extdata.tar.zst")
            if not os.path.isfile(ext_tar):
                ext_tar = os.path.join(pkg_dir, "extdata.tar.gz")
            if options.get("restore_extdata", True) and entry.get("has_extdata") and os.path.isfile(ext_tar):
                expected = entry.get("checksums", {}).get(os.path.basename(ext_tar))
                handle = _restore_data_tar(
                    serial, pkg, pkg, ext_tar, "/sdcard/Android/data", False, log,
                    should_cancel=should_cancel, expected_sha256=expected,
                    checksum_required=schema >= 3,
                    expected_uncompressed=entry.get("uncompressed_bytes", {}).get("extdata", 0),
                    defer_finalize=True,
                )
                transaction_handles.append(handle)
                log(f"  Restored external data (pending app commit): {pkg}")

            obb_tar = os.path.join(pkg_dir, "obb.tar.zst")
            if not os.path.isfile(obb_tar):
                obb_tar = os.path.join(pkg_dir, "obb.tar.gz")
            if options.get("restore_obb", True) and entry.get("has_obb") and os.path.isfile(obb_tar):
                expected = entry.get("checksums", {}).get(os.path.basename(obb_tar))
                handle = _restore_data_tar(
                    serial, pkg, pkg, obb_tar, "/sdcard/Android/obb", False, log,
                    should_cancel=should_cancel, expected_sha256=expected,
                    checksum_required=schema >= 3,
                    expected_uncompressed=entry.get("uncompressed_bytes", {}).get("obb", 0),
                    defer_finalize=True,
                )
                transaction_handles.append(handle)
                log(f"  Restored OBB cache (pending app commit): {pkg}")

            if options.get("restore_permissions", True) and entry.get("permissions"):
                try:
                    permissions_before = set(adbu.get_granted_permissions(serial, pkg))
                except Exception:
                    permissions_before = set()
                granted = 0
                for perm in entry["permissions"]:
                    if should_cancel():
                        raise adbu.OperationCancelled("Restore cancelled.")
                    if adbu.grant_package_permission(serial, pkg, perm):
                        granted += 1
                        if perm not in permissions_before:
                            newly_granted_permissions.append(perm)
                    else:
                        permission_failures.append(perm)
                log(f"  Auto-granted {granted}/{len(entry['permissions'])} runtime permissions.")

            # All selected destructive components for this app are ready. The
            # device group marker is the durable commit decision shared by Data,
            # ExtData/OBB and the PC-side APK rollback journal.
            group_id = None
            if transaction_handles:
                group_id = f"app_{uuid.uuid4().hex[:12]}"
                for handle in transaction_handles:
                    handle["group_id"] = group_id
                    journal = {k: v for k, v in handle.items() if k != "opid"}
                    journal["state"] = "PENDING_FINALIZE"
                    _write_journal(serial, handle["opid"], journal)
                if apk_txn:
                    _persist_apk_txn(apk_txn, state="PENDING_GROUP", group_id=group_id)
                _mark_group_commit(serial, group_id)
                commit_decided = True

                cleanup_complete = True
                for handle in transaction_handles:
                    try:
                        cleanup_complete = _finalize_commit(serial, handle) and cleanup_complete
                    except Exception as exc:
                        cleanup_complete = False
                        log(f"  [Notice] Commit succeeded; rollback cleanup is pending: {exc}")
                transaction_handles.clear()

                local_cleanup_ok = True
                if apk_txn:
                    try:
                        _persist_apk_txn(apk_txn, state="COMMITTED", group_id=group_id)
                        _cleanup_apk_txn(apk_txn)
                        local_cleanup_ok = not os.path.exists(apk_txn.get("_dir", ""))
                    except Exception as exc:
                        local_cleanup_ok = False
                        log(f"  [Notice] APK rollback snapshot cleanup is pending: {exc}")
                if cleanup_complete and local_cleanup_ok:
                    _remove_group_commit(serial, group_id)
                else:
                    log("  [Notice] Restore is committed; cleanup will be retried on the next device scan.")
            else:
                # APK-only / permission-only package restore has no device data
                # transaction. Commit the durable PC APK journal last.
                if apk_txn:
                    _persist_apk_txn(apk_txn, state="COMMITTED")
                    commit_decided = True
                    _cleanup_apk_txn(apk_txn)

            if permission_failures:
                result.failed.append(
                    f"{pkg}: {len(permission_failures)} runtime permission(s) could not be granted"
                )
            result.succeeded.append(pkg)
            log(f"  Finished: {pkg}")

        except adbu.OperationCancelled:
            component_ok = True
            if not commit_decided:
                for handle in reversed(transaction_handles):
                    component_ok = _rollback_commit(serial, handle, log) and component_ok
                for perm in reversed(newly_granted_permissions):
                    try:
                        adbu.revoke_package_permission(serial, pkg, perm)
                    except Exception:
                        component_ok = False
                apk_ok = True
                if apk_txn:
                    apk_ok = _restore_old_apks(
                        serial, pkg, _txn_old_apks(apk_txn), bool(apk_txn.get("was_installed")), log
                    )
                    if apk_ok:
                        _cleanup_apk_txn(apk_txn)
                package_rolled_back = component_ok and apk_ok
                if package_rolled_back:
                    result.rolled_back.append(pkg)
            result.cancelled = True
            raise
        except Exception as exc:
            package_failed = True
            component_ok = True
            if not commit_decided:
                for handle in reversed(transaction_handles):
                    component_ok = _rollback_commit(serial, handle, log) and component_ok
                for perm in reversed(newly_granted_permissions):
                    try:
                        if not adbu.revoke_package_permission(serial, pkg, perm):
                            component_ok = False
                    except Exception:
                        component_ok = False
                apk_ok = True
                if apk_txn:
                    apk_ok = _restore_old_apks(
                        serial, pkg, _txn_old_apks(apk_txn), bool(apk_txn.get("was_installed")), log
                    )
                    if apk_ok:
                        _cleanup_apk_txn(apk_txn)
                package_rolled_back = component_ok and apk_ok
                if package_rolled_back:
                    result.rolled_back.append(pkg)
            result.failed.append(f"{pkg}: {exc}")
            log(f"  [Failed] {pkg}: {exc}")
        finally:
            progress(idx, total)

        if package_failed and not package_rolled_back and not commit_decided:
            log(f"  [Warning] Full package rollback could not be confirmed for {pkg}.")
    return result

def _remote_file_size(serial, path):
    proc = adbu.adb_shell(serial, f"stat -c %s {shlex.quote(path)} 2>/dev/null",
                          root=False, check=False, timeout=15)
    out = (proc.stdout or "").strip()
    return int(out) if out.isdigit() else -1


def _restore_storage_tree(serial, local_dir, remote_root, label, log, should_cancel=lambda: False):
    result = RestoreResult()
    if not os.path.isdir(local_dir):
        return result
    log(f"Restoring {label} to {remote_root} ...")
    success_count = 0
    failure_count = 0
    skipped_count = 0
    opid = uuid.uuid4().hex[:10]
    temp_root = f"{remote_root.rstrip('/')}/.droidvault_tmp_{opid}"
    mk_tmp = adbu.adb_shell(serial, f"mkdir -p {shlex.quote(temp_root)}", root=False, check=False, timeout=20)
    if mk_tmp.returncode != 0:
        result.failed.append(f"{label}: could not create temporary namespace on target storage")
        return result
    try:
        for root, dirs, files in os.walk(local_dir):
            if should_cancel():
                result.cancelled = True
                raise adbu.OperationCancelled("Storage restore cancelled.")
            rel_dir = os.path.relpath(root, local_dir)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            remote_dir = remote_root.rstrip("/") + (f"/{rel_dir}" if rel_dir else "")
            mk = adbu.adb_shell(serial, f"mkdir -p {shlex.quote(remote_dir)}", root=False, check=False, timeout=20)
            if mk.returncode != 0:
                failure_count += 1
                if len(result.failed) < 200:
                    result.failed.append(f"{rel_dir or '/'}: could not create directory")
                continue
            for name in files:
                if should_cancel():
                    result.cancelled = True
                    raise adbu.OperationCancelled("Storage restore cancelled.")
                local_path = os.path.join(root, name)
                if os.path.islink(local_path):
                    skipped_count += 1
                    if len(result.skipped) < 100:
                        result.skipped.append(f"{rel_dir}/{name}: local symlink skipped")
                    continue
                target = f"{remote_dir}/{name}"
                type_check = adbu.adb_shell(
                    serial,
                    f"if [ -L {shlex.quote(target)} ]; then echo LINK; elif [ -d {shlex.quote(target)} ]; then echo DIR; elif [ -e {shlex.quote(target)} ]; then echo FILE; else echo MISSING; fi",
                    root=False, check=False, timeout=15,
                )
                target_type = (type_check.stdout or "").strip()
                if target_type in ("LINK", "DIR"):
                    failure_count += 1
                    if len(result.failed) < 200:
                        result.failed.append(f"{rel_dir}/{name}: refusing to replace existing {target_type.lower()}")
                    continue
                temp = f"{temp_root}/{uuid.uuid4().hex}.part"
                local_size = os.path.getsize(local_path)
                log(f"  Pushing: {(rel_dir + '/' if rel_dir else '')}{name} ...")
                try:
                    free_mb = adbu.get_free_space_mb(serial, remote_dir)
                    if free_mb <= 0:
                        raise RuntimeError("could not determine destination free space; refusing fail-open transfer")
                    required_bytes = local_size + 64 * 1024 * 1024
                    if free_mb * 1024 * 1024 < required_bytes:
                        raise RuntimeError(
                            f"insufficient destination space ({free_mb} MB free, "
                            f"need about {required_bytes // (1024 * 1024)} MB including safety reserve)"
                        )
                    proc = adbu.push_cancellable(serial, local_path, temp, should_cancel=should_cancel)
                    if proc.returncode != 0:
                        raise RuntimeError((proc.stderr or proc.stdout or "adb push failed").strip()[:400])
                    remote_size = _remote_file_size(serial, temp)
                    if remote_size != local_size:
                        raise RuntimeError(f"size verification failed ({remote_size} != {local_size})")
                    # Preserve an existing destination until the new file is fully
                    # present. The device-side trap restores it if the commit shell
                    # is interrupted after moving the old file aside.
                    txn_id = uuid.uuid4().hex[:10]
                    rollback = f"{target}.droidvault_rollback_{opid}_{txn_id[:6]}"
                    journal_path = f"{temp_root}/txn_{txn_id}.json"
                    payload = json.dumps(
                        {"target": target, "rollback": rollback, "old_exists": target_type == "FILE"},
                        ensure_ascii=True, separators=(",", ":"),
                    )
                    jproc = adbu.adb_shell(
                        serial, f"printf %s {shlex.quote(payload)} > {shlex.quote(journal_path)}",
                        root=False, check=False, timeout=20,
                    )
                    if jproc.returncode != 0:
                        raise RuntimeError("could not create storage transaction journal")
                    commit_cmd = (
                        f"T={shlex.quote(target)}; N={shlex.quote(temp)}; R={shlex.quote(rollback)}; J={shlex.quote(journal_path)}; MOVED=0; "
                        'rollback(){ if [ "$MOVED" = 1 ] && [ -e "$R" ]; then '
                        'rm -f -- "$T" 2>/dev/null; if mv "$R" "$T" 2>/dev/null; then rm -f -- "$J"; fi; fi; }; '
                        'trap rollback EXIT HUP INT TERM; '
                        'if [ -e "$T" ]; then mv "$T" "$R" || exit 41; MOVED=1; fi; '
                        'if mv "$N" "$T"; then MOVED=0; rm -f -- "$R" "$J"; trap - EXIT HUP INT TERM; exit 0; '
                        'else exit 42; fi'
                    )
                    mv = adbu.adb_shell(serial, commit_cmd, root=False, check=False, timeout=45)
                    if mv.returncode != 0:
                        raise RuntimeError((mv.stderr or mv.stdout or "safe file commit failed").strip()[:400])
                    success_count += 1
                except adbu.OperationCancelled:
                    raise
                except Exception as exc:
                    adbu.adb_shell(serial, f"rm -f {shlex.quote(temp)}", root=False, check=False, timeout=10)
                    # If a journal was created, recover it immediately when possible.
                    # If ADB is gone, leave the namespace for detect-time recovery.
                    try:
                        adbu.cleanup_stale_storage_temp(serial, remote_root, older_than_seconds=0)
                    except Exception:
                        pass
                    failure_count += 1
                    if len(result.failed) < 200:
                        result.failed.append(f"{rel_dir}/{name}: {exc}")
    finally:
        # The recovery helper removes the namespace only when no unresolved
        # transaction journal remains. Never blindly delete rollback metadata.
        try:
            adbu.cleanup_stale_storage_temp(serial, remote_root, older_than_seconds=0)
        except Exception:
            pass

    result.succeeded.append(f"{label}:{success_count} files committed")
    if failure_count > len(result.failed):
        result.failed.append(f"{label}:{failure_count - len(result.failed)} additional failures omitted from memory")
    if skipped_count > len(result.skipped):
        result.skipped.append(f"{label}:{skipped_count - len(result.skipped)} additional skips omitted from memory")
    return result


def restore_internal_storage(serial, session_dir, log=lambda m: None, should_cancel=lambda: False):
    return _restore_storage_tree(
        serial, os.path.join(session_dir, "internal_storage"), "/sdcard",
        "InternalStorage", log, should_cancel,
    )


def restore_external_storage(serial, session_dir, sd_path, log=lambda m: None, should_cancel=lambda: False):
    if not sd_path:
        return RestoreResult(skipped=["ExternalStorage:no SD card target"])
    return _restore_storage_tree(
        serial, os.path.join(session_dir, "external_storage"), sd_path,
        "ExternalStorage", log, should_cancel,
    )


def run_restore(serial, session_dir, manifest, selected_packages, options,
                restore_internal=False, restore_external=False, external_sd_path=None,
                log=lambda m: None, progress=lambda i, total: None,
                should_cancel=lambda: False):
    result = RestoreResult()
    root_required = bool(
        (selected_packages and (options.get("restore_data") or options.get("restore_extdata")
                                or options.get("restore_obb") or options.get("restore_permissions")))
        or options.get("restore_wifi") or options.get("restore_sms") or options.get("restore_contacts")
    )
    if root_required and not adbu.check_root(serial):
        raise RuntimeError("Selected restore components require root, but root access is not available.")

    opid = uuid.uuid4().hex[:12]
    lock_id = f"restore-{opid}"
    if not adbu.acquire_device_operation_lock(serial, lock_id):
        raise RuntimeError("Another DroidVault operation is already active on this device.")
    lock_heartbeat = adbu.start_device_lock_heartbeat(serial, lock_id)
    try:
        # APK recovery must run before device-journal cleanup: a surviving group
        # commit marker tells the local journal that the new APK belongs to a
        # committed app transaction.
        recover_local_apk_transactions(serial, log)
        recover_incomplete_transactions(serial, log)
        if should_cancel():
            raise adbu.OperationCancelled("Restore cancelled.")

        restore_any_personal = options.get("restore_wifi") or options.get("restore_sms") or options.get("restore_contacts")
        if restore_any_personal:
            result.merge(restore_system_personal_data(
                serial, session_dir, manifest, options, log=log, should_cancel=should_cancel
            ))
        if selected_packages:
            result.merge(restore_apps(
                serial, session_dir, manifest, selected_packages, options,
                log=log, progress=progress, should_cancel=should_cancel,
            ))
        if restore_internal:
            result.merge(restore_internal_storage(serial, session_dir, log, should_cancel))
        if restore_external:
            result.merge(restore_external_storage(serial, session_dir, external_sd_path, log, should_cancel))
        return result
    except adbu.OperationCancelled:
        result.cancelled = True
        raise
    finally:
        adbu.stop_device_lock_heartbeat(lock_heartbeat)
        adbu.release_device_operation_lock(serial, lock_id)
