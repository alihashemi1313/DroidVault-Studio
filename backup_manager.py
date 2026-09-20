import json
import os
import re
import shutil
import uuid
from datetime import datetime

import adb_utils as adbu

SCHEMA_VERSION = 3
MIN_PC_RESERVE_BYTES = 512 * 1024 * 1024


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ensure_pc_space(path, expected_bytes=0):
    base = path if os.path.isdir(path) else os.path.dirname(path) or "."
    usage = shutil.disk_usage(base)
    needed = int(expected_bytes or 0) + MIN_PC_RESERVE_BYTES
    if usage.free < needed:
        raise RuntimeError(
            f"Insufficient PC disk space. Free: {adbu.format_size(usage.free)}, "
            f"required safety budget: {adbu.format_size(needed)}."
        )


def _package_from_data_base(base):
    for pkg in (
        "com.android.providers.telephony",
        "com.android.providers.contacts",
        "com.samsung.android.providers.contacts",
        "com.sec.android.provider.logsprovider",
    ):
        if pkg in base:
            return pkg
    return ""


def _safe_local_name(remote_path, index):
    base = os.path.basename(remote_path) or "config"
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
    return f"wifi_{index:02d}_{base}"


def _backup_one_app(serial, pkg_info, apps_dir, options, log, speed_cb=None,
                    should_cancel=lambda: False):
    pkg = pkg_info["package"]
    use_zstd = bool(options.get("effective_use_zstd", options.get("use_zstd", False)))
    zstd_mode = options.get("zstd_mode", "device" if use_zstd else "gzip")
    compress_on_pc = use_zstd and zstd_mode == "pc"
    tar_ext = "tar.zst" if use_zstd else "tar.gz"
    entry = {
        "package": pkg,
        "is_system": pkg_info.get("is_system", False),
        "apk_files": [],
        "has_data": False,
        "has_extdata": False,
        "has_obb": False,
        "compression": "zstd" if use_zstd else "gzip",
        "permissions": [],
        "checksums": {},
        "uncompressed_bytes": {},
        "component_status": {},
        "warnings": [],
    }

    try:
        entry.update(adbu.get_package_version(serial, pkg))
    except Exception as exc:
        entry["warnings"].append(f"Version metadata: {exc}")

    if options.get("include_permissions", True):
        try:
            entry["permissions"] = adbu.get_granted_permissions(serial, pkg)
        except Exception as exc:
            entry["warnings"].append(f"Permissions: {exc}")

    pkg_dir = os.path.join(apps_dir, pkg)
    os.makedirs(pkg_dir, exist_ok=True)

    # APK files: pull to .part and commit only on successful adb exit.
    if options.get("include_apk", True):
        apk_dir = os.path.join(pkg_dir, "apk")
        os.makedirs(apk_dir, exist_ok=True)
        apk_ok = True
        try:
            paths = adbu.get_apk_paths(serial, pkg)
            if not paths:
                apk_ok = False
                entry["warnings"].append("APK path not found.")
            for remote_apk in paths:
                if should_cancel():
                    raise adbu.OperationCancelled("Backup cancelled.")
                local_name = os.path.basename(remote_apk)
                local_path = os.path.join(apk_dir, local_name)
                part_path = local_path + ".part"
                _ensure_pc_space(apk_dir)
                proc = adbu.pull_cancellable(serial, remote_apk, part_path, should_cancel=should_cancel)
                if proc.returncode != 0 or not os.path.isfile(part_path) or os.path.getsize(part_path) <= 0:
                    apk_ok = False
                    try:
                        os.remove(part_path)
                    except OSError:
                        pass
                    entry["warnings"].append(f"APK pull failed: {local_name}: {(proc.stderr or '').strip()[:300]}")
                    continue
                os.replace(part_path, local_path)
                entry["apk_files"].append(local_name)
                entry["checksums"][f"apk/{local_name}"] = adbu.calculate_sha256(local_path)
                try:
                    adbu.extract_icon_from_local_apk(local_path, pkg)
                except Exception:
                    pass
        except adbu.OperationCancelled:
            raise
        except Exception as exc:
            apk_ok = False
            entry["warnings"].append(f"APK error: {exc}")
        if not apk_ok:
            # A split APK set is atomic: a subset is not a valid restorable app.
            # Remove partial files rather than advertising an incomplete set later.
            for local_name in list(entry["apk_files"]):
                try:
                    os.remove(os.path.join(apk_dir, local_name))
                except OSError:
                    pass
                entry["checksums"].pop(f"apk/{local_name}", None)
            entry["apk_files"].clear()
        entry["component_status"]["apk"] = "ok" if apk_ok and entry["apk_files"] else "failed"

    # Quiesce once before any mutable app-owned data snapshot. This also covers
    # the valid case where Data is unchecked but ExtData/OBB is selected.
    needs_quiesce = bool(options.get("include_data", True) or options.get("include_extdata", False) or options.get("include_obb", False))
    quiesced = True
    if needs_quiesce:
        try:
            stop_proc = adbu.force_stop(serial, pkg)
            quiesced = stop_proc.returncode == 0
            if not quiesced:
                entry["warnings"].append(f"Could not quiesce app before data snapshot: {(stop_proc.stderr or stop_proc.stdout or '').strip()[:300]}")
        except Exception as exc:
            quiesced = False
            entry["warnings"].append(f"Could not quiesce app before data snapshot: {exc}")

    # Internal Data: stream after quiescing. No temp archive is created on the phone.
    if options.get("include_data", True) and quiesced:
        local_tar = os.path.join(pkg_dir, f"data.{tar_ext}")
        try:
            source = f"/data/data/{pkg}"
            size = int(pkg_info.get("_known_data_size") or 0)
            if size <= 0:
                size = adbu.get_remote_size_bytes(serial, source, root=True)
            entry["uncompressed_bytes"]["data"] = size
            # Streaming enforces the PC free-space reserve continuously. Requiring
            # room for the full uncompressed source would reject backups that
            # compress well and would effectively remove a useful feature.
            _ensure_pc_space(pkg_dir)
            ok = adbu.stream_tar_pull(
                serial, "/data/data", pkg, local_tar, use_zstd=use_zstd,
                speed_cb=speed_cb, should_cancel=should_cancel,
                compress_on_pc=compress_on_pc,
                # /data/data/<pkg>/lib is package-manager/native-library state,
                # commonly an absolute symlink into /data/app. Do not archive it
                # as user data; the installed APK recreates the correct target.
                exclude_members=[f"{pkg}/lib", f"{pkg}/lib/*"],
            )
            if ok:
                entry["checksums"][f"data.{tar_ext}"] = adbu.calculate_sha256(local_tar)
                entry["has_data"] = True
                entry["component_status"]["data"] = "ok"
        except adbu.OperationCancelled:
            raise
        except Exception as exc:
            try:
                if os.path.exists(local_tar):
                    os.remove(local_tar)
            except OSError:
                pass
            entry["has_data"] = False
            entry["checksums"].pop(f"data.{tar_ext}", None)
            entry["component_status"]["data"] = "failed"
            entry["warnings"].append(f"Data error: {exc}")

    if options.get("include_extdata", False) and quiesced:
        ext_path = f"/sdcard/Android/data/{pkg}"
        if adbu.path_exists(serial, ext_path, root=True):
            local_tar = os.path.join(pkg_dir, f"extdata.{tar_ext}")
            try:
                size = adbu.get_remote_size_bytes(serial, ext_path, root=True)
                entry["uncompressed_bytes"]["extdata"] = size
                _ensure_pc_space(pkg_dir)
                ok = adbu.stream_tar_pull(
                    serial, "/sdcard/Android/data", pkg, local_tar, use_zstd=use_zstd,
                    speed_cb=speed_cb, should_cancel=should_cancel,
                    compress_on_pc=compress_on_pc,
                )
                if ok:
                    entry["checksums"][f"extdata.{tar_ext}"] = adbu.calculate_sha256(local_tar)
                    entry["has_extdata"] = True
                    entry["component_status"]["extdata"] = "ok"
            except adbu.OperationCancelled:
                raise
            except Exception as exc:
                try:
                    if os.path.exists(local_tar):
                        os.remove(local_tar)
                except OSError:
                    pass
                entry["has_extdata"] = False
                entry["checksums"].pop(f"extdata.{tar_ext}", None)
                entry["component_status"]["extdata"] = "failed"
                entry["warnings"].append(f"External data error: {exc}")
        else:
            entry["component_status"]["extdata"] = "missing"

    if options.get("include_obb", False) and quiesced:
        obb_path = f"/sdcard/Android/obb/{pkg}"
        if adbu.path_exists(serial, obb_path, root=True):
            local_tar = os.path.join(pkg_dir, f"obb.{tar_ext}")
            try:
                size = adbu.get_remote_size_bytes(serial, obb_path, root=True)
                entry["uncompressed_bytes"]["obb"] = size
                _ensure_pc_space(pkg_dir)
                ok = adbu.stream_tar_pull(
                    serial, "/sdcard/Android/obb", pkg, local_tar, use_zstd=use_zstd,
                    speed_cb=speed_cb, should_cancel=should_cancel,
                    compress_on_pc=compress_on_pc,
                )
                if ok:
                    entry["checksums"][f"obb.{tar_ext}"] = adbu.calculate_sha256(local_tar)
                    entry["has_obb"] = True
                    entry["component_status"]["obb"] = "ok"
            except adbu.OperationCancelled:
                raise
            except Exception as exc:
                try:
                    if os.path.exists(local_tar):
                        os.remove(local_tar)
                except OSError:
                    pass
                entry["has_obb"] = False
                entry["checksums"].pop(f"obb.{tar_ext}", None)
                entry["component_status"]["obb"] = "failed"
                entry["warnings"].append(f"OBB error: {exc}")
        else:
            entry["component_status"]["obb"] = "missing"

    if needs_quiesce and not quiesced:
        if options.get("include_data", True):
            entry["component_status"]["data"] = "failed"
        if options.get("include_extdata", False):
            entry["component_status"]["extdata"] = "failed"
        if options.get("include_obb", False):
            entry["component_status"]["obb"] = "failed"

    return entry


# جایگزین تابع _find_active_database_folder در backup_manager_2.py
def _find_active_database_folder(serial, candidate_paths):
    """Find the provider DB directory by checking direct file existence reliably."""
    for candidate in candidate_paths:
        if len(candidate) == 4:
            package, base, folder, required_names = candidate
        else:
            package, base, folder = candidate
            required_names = ()
        full = f"{base}/{folder}"
        if not adbu.path_exists(serial, full, root=True):
            continue
        if required_names:
            if any(adbu.path_exists(serial, f"{full}/{name}", root=True) for name in required_names):
                return package, base, folder, tuple(required_names)
        else:
            proc = adbu.adb_shell(serial, f"ls -1 {shlex_quote(full)}/*.db 2>/dev/null | head -n 1", root=True, check=False, timeout=15)
            if (proc.stdout or "").strip():
                return package, base, folder, ()
    return None, None, None, ()


def _database_state_signature(serial, base, folder, names):
    """Small stability fingerprint used when a persistent provider cannot be force-stopped."""
    full = f"{base}/{folder}"
    pieces = []
    for name in names or ():
        for suffix in ("", "-wal", "-shm"):
            path = f"{full}/{name}{suffix}"
            proc = adbu.adb_shell(
                serial, f"stat -c '%n:%s:%Y' {shlex_quote(path)} 2>/dev/null || true",
                root=True, check=False, timeout=15,
            )
            value = (proc.stdout or "").strip()
            if value:
                pieces.append(value)
    return "|".join(sorted(pieces))


def shlex_quote(value):
    # Kept local to avoid importing shlex in multiple call sites.
    import shlex
    return shlex.quote(value)


def _backup_wifi(serial, sys_dir, log, should_cancel=lambda: False):
    candidates = [
        "/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml",
        "/data/misc/wifi/WifiConfigStore.xml",
        "/data/misc/wifi/wpa_supplicant.conf",
    ]
    result = {"included": False, "files": [], "warnings": []}
    for index, remote_path in enumerate(candidates):
        if should_cancel():
            raise adbu.OperationCancelled("Backup cancelled.")
        if not adbu.path_exists(serial, remote_path, root=True):
            continue
        local_name = _safe_local_name(remote_path, index)
        local_path = os.path.join(sys_dir, local_name)
        try:
            adbu.stream_root_command_to_file(
                serial, f"cat {shlex_quote(remote_path)}", local_path,
                should_cancel=should_cancel, min_size=10,
            )
            meta = {
                "source_path": remote_path,
                "file": local_name,
                "sha256": adbu.calculate_sha256(local_path),
                "size": os.path.getsize(local_path),
            }
            result["files"].append(meta)
            result["included"] = True
            log(f"  Wi-Fi config saved ({remote_path}).")
        except adbu.OperationCancelled:
            raise
        except Exception as exc:
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                pass
            result["warnings"].append(f"{remote_path}: {exc}")
    if not result["included"]:
        log("  [Notice] No active Wi-Fi configuration found.")
    return result


def _backup_database(serial, sys_dir, output_name, candidates, log, label,
                     should_cancel=lambda: False):
    package, base, folder, database_files = _find_active_database_folder(serial, candidates)
    result = {
        "included": False, "source_package": package or "", "source_base": base or "",
        "folder": folder or "", "database_files": list(database_files), "warnings": [],
    }
    if package:
        try:
            result["source_version"] = adbu.get_package_version(serial, package)
        except Exception:
            result["source_version"] = {}
    if not base:
        log(f"  [Notice] {label} database not found or empty.")
        return result
    local_path = os.path.join(sys_dir, output_name)
    try:
        quiesced = False
        if package:
            try:
                stop_proc = adbu.force_stop(serial, package)
                quiesced = stop_proc.returncode == 0
                if not quiesced:
                    msg = (stop_proc.stderr or stop_proc.stdout or "provider remained active").strip()[:300]
                    result["warnings"].append(f"force-stop unavailable: {msg}")
                    log(f"  [Notice] {label} provider could not be force-stopped; using stability-verified snapshot.")
            except Exception as exc:
                result["warnings"].append(f"force-stop unavailable: {exc}")
                log(f"  [Notice] {label} provider could not be force-stopped; using stability-verified snapshot.")

        size = adbu.get_remote_size_bytes(serial, f"{base}/{folder}", root=True)
        _ensure_pc_space(sys_dir, size)
        attempts = 1 if quiesced else 2
        last_error = None
        for attempt in range(attempts):
            if should_cancel():
                raise adbu.OperationCancelled("Backup cancelled.")
            before = _database_state_signature(serial, base, folder, database_files) if not quiesced else ""
            try:
                adbu.stream_tar_pull(
                    serial, base, folder, local_path, use_zstd=False,
                    should_cancel=should_cancel,
                )
            except Exception as exc:
                last_error = exc
                continue
            after = _database_state_signature(serial, base, folder, database_files) if not quiesced else before
            if quiesced or (before and before == after):
                last_error = None
                break
            # اگر آرشیو دانلود شده از نظر ساختار tar سالم است، به خاطر تغییر ژورنال آن را پاک نمی‌کنیم
            ok, _ = adbu.validate_local_tar_archive(local_path, expected_top=folder)
            if ok:
                last_error = None
                log(f"  [Notice] {label} snapshot archived with active WAL journal.")
                break
            last_error = RuntimeError(f"{label} database archive corrupted during active read")
            try:
                os.remove(local_path)
            except OSError:
                pass
        if last_error is not None:
            raise last_error

        result.update({
            "included": True,
            "file": output_name,
            "sha256": adbu.calculate_sha256(local_path),
            "uncompressed_bytes": size,
            "snapshot_mode": "quiesced" if quiesced else "stability_verified",
        })
        log(f"  {label} database archived from {base}.")
    except adbu.OperationCancelled:
        raise
    except Exception as exc:
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except OSError:
            pass
        result["error"] = str(exc)
        log(f"  [Warning] {label} backup failed: {exc}")
    return result


def _backup_sms(serial, sys_dir, log, should_cancel=lambda: False):
    candidates = [
        ("com.android.providers.telephony", "/data/user_de/0/com.android.providers.telephony", "databases", ("mmssms.db",)),
        ("com.android.providers.telephony", "/data/data/com.android.providers.telephony", "databases", ("mmssms.db",)),
        ("com.android.providers.telephony", "/data/user/0/com.android.providers.telephony", "databases", ("mmssms.db",)),
    ]
    return _backup_database(serial, sys_dir, "sms_databases.tar.gz", candidates, log, "SMS/MMS", should_cancel)


def _backup_contacts_and_calls(serial, sys_dir, log, should_cancel=lambda: False):
    contacts_candidates = [
        ("com.android.providers.contacts", "/data/user_de/0/com.android.providers.contacts", "databases", ("contacts2.db",)),
        ("com.android.providers.contacts", "/data/data/com.android.providers.contacts", "databases", ("contacts2.db",)),
        ("com.android.providers.contacts", "/data/user/0/com.android.providers.contacts", "databases", ("contacts2.db",)),
        ("com.samsung.android.providers.contacts", "/data/user_de/0/com.samsung.android.providers.contacts", "databases", ("contacts2.db",)),
        ("com.samsung.android.providers.contacts", "/data/data/com.samsung.android.providers.contacts", "databases", ("contacts2.db",)),
    ]
    samsung_candidates = [
        ("com.sec.android.provider.logsprovider", "/data/user_de/0/com.sec.android.provider.logsprovider", "databases", ("logs.db",)),
        ("com.sec.android.provider.logsprovider", "/data/data/com.sec.android.provider.logsprovider", "databases", ("logs.db",)),
        ("com.sec.android.provider.logsprovider", "/data/user/0/com.sec.android.provider.logsprovider", "databases", ("logs.db",)),
    ]

    res = {}
    # 1. بکاپ کانتکت‌ها (شامل calllog.db استاندارد در اندروید 10 به بعد)
    res["contacts"] = _backup_database(
        serial, sys_dir, "contacts_databases.tar.gz", contacts_candidates,
        log, "Contacts & Call History", should_cancel,
    )

    # 2. فقط در صورتی به دنبال پکیج اختصاصی سامسونگ می‌گردیم که واقعاً روی گوشی نصب و فعال باشد
    s_pkg, s_base, s_folder, _ = _find_active_database_folder(serial, samsung_candidates)
    if s_base:
        res["samsung_calllogs"] = _backup_database(
            serial, sys_dir, "samsung_calllogs.tar.gz", samsung_candidates,
            log, "Samsung Legacy Call Logs", should_cancel,
        )
    else:
        # اگر پکیج سامسونگ نبود، لاگ هشدار ندهد چون تماس‌ها در همان دیتابیس کانتکت بالا ذخیره شده‌اند
        res["samsung_calllogs"] = {"included": False, "integrated_in_contacts": True}

    return res

def _backup_storage_tree(serial, remote_root, local_dir, log, should_cancel,
                         skip_android_app_data=False):
    os.makedirs(local_dir, exist_ok=True)
    result = {"status": "complete", "files_ok": 0, "files_failed": 0, "failures": []}
    entries = adbu.list_dir(serial, remote_root, root=False, strict=True)
    for entry_name in entries:
        if should_cancel():
            raise adbu.OperationCancelled("Storage backup cancelled.")
        entry_name = entry_name.strip()
        if not entry_name:
            continue
        remote_path = f"{remote_root}/{entry_name}"

        if entry_name == "Android" and skip_android_app_data:
            android_local = os.path.join(local_dir, "Android")
            os.makedirs(android_local, exist_ok=True)
            children = adbu.list_dir(serial, remote_path, root=False, strict=True)
            for child in children:
                if should_cancel():
                    raise adbu.OperationCancelled("Storage backup cancelled.")
                child = child.strip()
                if not child or child in ("data", "obb"):
                    continue
                _ensure_pc_space(android_local)
                proc = adbu.pull_cancellable(
                    serial, f"{remote_path}/{child}", android_local,
                    should_cancel=should_cancel,
                )
                if proc.returncode == 0:
                    result["files_ok"] += 1
                else:
                    result["files_failed"] += 1
                    partial = os.path.join(android_local, os.path.basename(child))
                    try:
                        if os.path.isdir(partial):
                            shutil.rmtree(partial)
                        elif os.path.exists(partial):
                            os.remove(partial)
                    except OSError:
                        pass
                    if len(result["failures"]) < 200:
                        result["failures"].append(f"Android/{child}: {(proc.stderr or '').strip()[:300]}")
            continue

        log(f"  Pulling Storage: {entry_name} ...")
        _ensure_pc_space(local_dir)
        proc = adbu.pull_cancellable(serial, remote_path, local_dir, should_cancel=should_cancel)
        if proc.returncode == 0:
            result["files_ok"] += 1
        else:
            result["files_failed"] += 1
            partial = os.path.join(local_dir, os.path.basename(entry_name))
            try:
                if os.path.isdir(partial):
                    shutil.rmtree(partial)
                elif os.path.exists(partial):
                    os.remove(partial)
            except OSError:
                pass
            if len(result["failures"]) < 200:
                result["failures"].append(f"{entry_name}: {(proc.stderr or '').strip()[:300]}")

    if result["files_failed"]:
        result["status"] = "partial"
    return result


def _system_component_errors(value, prefix):
    errors = []
    if isinstance(value, dict):
        if value.get("error"):
            errors.append(f"{prefix}: {value['error']}")
        for warning in value.get("warnings", []) if isinstance(value.get("warnings"), list) else []:
            errors.append(f"{prefix}: {warning}")
        # Nested contacts/call-log structure.
        for key, child in value.items():
            if isinstance(child, dict) and key not in ("source_version",):
                errors.extend(_system_component_errors(child, f"{prefix}/{key}"))
    return errors


def run_backup(serial, packages, dest_root, options, log=lambda m: None,
               progress=lambda i, total, speed_str: None, should_cancel=lambda: False):
    os.makedirs(dest_root, exist_ok=True)
    _ensure_pc_space(dest_root)

    root_required = bool(
        options.get("include_wifi") or options.get("include_sms") or options.get("include_contacts")
        or (packages and (options.get("include_data") or options.get("include_extdata") or options.get("include_obb")))
    )
    if root_required and not adbu.check_root(serial):
        raise RuntimeError("Selected backup components require root, but root access is not available.")

    requested_zstd = bool(options.get("use_zstd"))
    # Prefer PC-side zstd when available: it preserves the feature while keeping
    # multi-core compression load and heat off the phone. Device zstd remains a
    # compatibility fallback when the PC has no zstd binary.
    pc_zstd = requested_zstd and adbu.local_zstd_available()
    device_zstd = requested_zstd and (not pc_zstd) and adbu.supports_tar_zstd(serial)
    effective_zstd = bool(requested_zstd and adbu.local_can_validate_zstd() and (device_zstd or pc_zstd))
    options = dict(options)
    options["effective_use_zstd"] = effective_zstd
    options["zstd_mode"] = "pc" if effective_zstd and pc_zstd else ("device" if effective_zstd and device_zstd else "gzip")
    if requested_zstd and options["zstd_mode"] == "pc":
        log("[Notice] Zstandard compression is running on the PC to reduce phone CPU/thermal overhead.")
    elif requested_zstd and options["zstd_mode"] == "device":
        log("[Notice] PC zstd is unavailable; using the device tar zstd implementation.")
    elif requested_zstd and not effective_zstd:
        log("[Notice] Zstandard is unavailable for a fully verifiable pipeline; falling back to gzip.")

    opid = uuid.uuid4().hex[:12]
    lock_id = f"backup-{opid}"
    if not adbu.acquire_device_operation_lock(serial, lock_id):
        raise RuntimeError("Another DroidVault operation is already active on this device.")
    lock_heartbeat = adbu.start_device_lock_heartbeat(serial, lock_id)

    try:
        base_name = datetime.now().strftime("session_%Y%m%d_%H%M%S") + f"_{uuid.uuid4().hex[:6]}"
        final_dir = os.path.join(dest_root, base_name)
        work_dir = final_dir + ".inprogress"
        apps_dir = os.path.join(work_dir, "apps")
        sys_dir = os.path.join(work_dir, "system_data")
        os.makedirs(apps_dir, exist_ok=False)
        os.makedirs(sys_dir, exist_ok=True)
        manifest_path = os.path.join(work_dir, "manifest.json")

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "in_progress",
            "result": "pending",
            "created_at": datetime.now().isoformat(),
            "operation_id": opid,
            "device": adbu.get_device_info(serial),
            "options": options,
            "apps": [],
            "system_data": {},
            "internal_storage_included": False,
            "external_storage_included": False,
            "internal_storage": {"status": "not_requested"},
            "external_storage": {"status": "not_requested"},
            "warnings": [],
        }
        _atomic_write_json(manifest_path, manifest)

        try:
            if should_cancel():
                raise adbu.OperationCancelled("Backup cancelled.")

            if options.get("include_wifi"):
                wifi_result = _backup_wifi(serial, sys_dir, log, should_cancel)
                manifest["system_data"]["wifi"] = wifi_result
                manifest["warnings"].extend(_system_component_errors(wifi_result, "wifi"))
                _atomic_write_json(manifest_path, manifest)

            if options.get("include_sms"):
                sms_result = _backup_sms(serial, sys_dir, log, should_cancel)
                manifest["system_data"]["sms"] = sms_result
                manifest["warnings"].extend(_system_component_errors(sms_result, "sms"))
                _atomic_write_json(manifest_path, manifest)

            if options.get("include_contacts"):
                contacts_result = _backup_contacts_and_calls(serial, sys_dir, log, should_cancel)
                manifest["system_data"]["contacts_and_calls"] = contacts_result
                manifest["warnings"].extend(_system_component_errors(contacts_result, "contacts_and_calls"))
                _atomic_write_json(manifest_path, manifest)

            total = len(packages)
            if total:
                log(f"Starting backup for {total} selected applications...")
            for idx, pkg_info in enumerate(packages, start=1):
                if should_cancel():
                    raise adbu.OperationCancelled("Backup cancelled.")
                pkg = pkg_info["package"]
                log(f"[{idx}/{total}] Archiving {pkg} ...")

                def speed_update(_bytes_done, mbps, index=idx):
                    progress(index, total, f"{mbps:.1f} MB/s")

                entry = _backup_one_app(
                    serial, pkg_info, apps_dir, options, log,
                    speed_cb=speed_update, should_cancel=should_cancel,
                )
                manifest["apps"].append(entry)
                if entry.get("warnings"):
                    manifest["warnings"].append({"package": pkg, "warnings": entry["warnings"]})
                _atomic_write_json(manifest_path, manifest)
                progress(idx, total, "Done")

            if options.get("include_internal_storage", False):
                log("Starting internal storage (/sdcard) backup...")
                result = _backup_storage_tree(
                    serial, "/sdcard", os.path.join(work_dir, "internal_storage"),
                    log, should_cancel,
                    skip_android_app_data=options.get("exclude_app_data_in_storage", True),
                )
                manifest["internal_storage"] = result
                manifest["internal_storage_included"] = result["status"] == "complete"
                if result["status"] != "complete":
                    manifest["warnings"].append({"internal_storage": result.get("failures", [])})
                _atomic_write_json(manifest_path, manifest)

            sd_path = options.get("external_sd_path")
            if options.get("include_external_storage", False) and sd_path:
                log(f"Backing up MicroSD Card from {sd_path} ...")
                result = _backup_storage_tree(
                    serial, sd_path, os.path.join(work_dir, "external_storage"),
                    log, should_cancel,
                )
                manifest["external_storage"] = result
                manifest["external_storage_included"] = result["status"] == "complete"
                if result["status"] != "complete":
                    manifest["warnings"].append({"external_storage": result.get("failures", [])})
                _atomic_write_json(manifest_path, manifest)

            manifest["status"] = "complete"
            manifest["result"] = "partial" if manifest["warnings"] else "success"
            manifest["completed_at"] = datetime.now().isoformat()
            _atomic_write_json(manifest_path, manifest)
            os.replace(work_dir, final_dir)
            log(f"Session committed: {final_dir}")
            return final_dir, manifest

        except adbu.OperationCancelled:
            manifest["status"] = "cancelled"
            manifest["result"] = "cancelled"
            manifest["completed_at"] = datetime.now().isoformat()
            try:
                _atomic_write_json(manifest_path, manifest)
            except Exception:
                pass
            log("Backup cancelled safely. Incomplete session was not committed.")
            raise
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["result"] = "failed"
            manifest["error"] = str(exc)
            manifest["completed_at"] = datetime.now().isoformat()
            try:
                _atomic_write_json(manifest_path, manifest)
            except Exception:
                pass
            raise
    finally:
        adbu.stop_device_lock_heartbeat(lock_heartbeat)
        adbu.release_device_operation_lock(serial, lock_id)
