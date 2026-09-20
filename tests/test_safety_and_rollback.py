# tests/test_safety_and_rollback.py
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# اضافه کردن ریشه پروژه به مسیر ایمپورت
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import adb_utils as adbu


class TestSecurityAndPathValidation(unittest.TestCase):
    """File name validation test against path traversal attacks"""

    def test_safe_member_name_blocking_traversal(self):
        # Checking for access prevention to paths outside the directory
        self.assertFalse(adbu._safe_member_name("../evil.sh"))
        self.assertFalse(adbu._safe_member_name("dir/../../etc/passwd"))
        self.assertFalse(adbu._safe_member_name("/system/bin/sh"))
        self.assertFalse(adbu._safe_member_name("C:\\Windows\\System32\\calc.exe"))
        self.assertFalse(adbu._safe_member_name("payload\x00.png"))
        self.assertFalse(adbu._safe_member_name(""))

    def test_safe_member_name_expected_top(self):
        # Root Directory Consistency Check
        self.assertTrue(adbu._safe_member_name("com.myapp/databases/db.sqlite", expected_top="com.myapp"))
        self.assertFalse(adbu._safe_member_name("com.otherapp/data.txt", expected_top="com.myapp"))

    def test_safe_member_name_valid_cases(self):
        self.assertTrue(adbu._safe_member_name("files/cache/sample.dat"))
        self.assertTrue(adbu._safe_member_name("databases/main.db"))


class TestArchiveIntegrityValidation(unittest.TestCase):
    """Health and safety assessment test of the archive structure prior to restoration"""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_reject_empty_or_small_archive(self):
        empty_file = os.path.join(self.test_dir.name, "empty.tar.gz")
        with open(empty_file, "wb") as f:
            f.write(b"")
        ok, reason = adbu.validate_local_tar_archive(empty_file)
        self.assertFalse(ok)
        self.assertIn("missing or empty", reason)

    def test_accept_valid_archive(self):
        valid_tar = os.path.join(self.test_dir.name, "valid.tar.gz")
        with tarfile.open(valid_tar, "w:gz") as tf:
            data = b"sample app database content"
            ti = tarfile.TarInfo(name="com.test.app/databases/app.db")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

        ok, reason = adbu.validate_local_tar_archive(valid_tar, expected_top="com.test.app")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_reject_archive_with_malicious_path_traversal(self):
        poisoned_tar = os.path.join(self.test_dir.name, "poison.tar.gz")
        with tarfile.open(poisoned_tar, "w:gz") as tf:
            data = b"malicious binary payload"
            ti = tarfile.TarInfo(name="../../system/bin/malware")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

        ok, reason = adbu.validate_local_tar_archive(poisoned_tar, expected_top="com.test.app")
        self.assertFalse(ok)
        self.assertIn("unsafe archive member", reason)


class TestAtomicOperationsAndFailureInjection(unittest.TestCase):
    """Failure simulation test and verification of temporary file (.part) cleanup."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()

    @patch("subprocess.Popen")
    def test_stream_tar_pull_cleans_up_part_file_on_error(self, mock_popen):
        # Simulating Errors and Failures During Streaming
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1
        mock_proc.returncode = 1
        mock_proc.stdout.read.return_value = b""
        mock_proc.stderr.read.return_value = b"I/O Device Error during backup"
        mock_proc.wait.return_value = 1
        mock_popen.return_value = mock_proc

        target_tar = os.path.join(self.test_dir.name, "backup.tar.gz")
        part_tar = target_tar + ".part"

        with self.assertRaises(adbu.AdbError):
            adbu.stream_tar_pull("emulator-5554", "/data/data", "com.pkg", target_tar)

        # بررسی گارانتی عدم باقی‌ماندن فایل ناتمام روی دیسک
        self.assertFalse(os.path.exists(target_tar), "Target tar should not be committed on error")
        self.assertFalse(os.path.exists(part_tar), ".part temporary file was not cleaned up after failure")

    def test_sha256_checksum_verification(self):
        sample_file = os.path.join(self.test_dir.name, "test_file.bin")
        data = b"Predictable deterministic content"
        with open(sample_file, "wb") as f:
            f.write(data)

        digest = adbu.calculate_sha256(sample_file)
        self.assertEqual(len(digest), 64)
        # تغییر در فایل باید هش را تغییر دهد
        with open(sample_file, "ab") as f:
            f.write(b"!")
        self.assertNotEqual(digest, adbu.calculate_sha256(sample_file))

class TestRollbackAndRecoveryMechanism(unittest.TestCase):
    """تست اعتبارسنجی عملیات بازگشت تراکنش (Rollback) در شرایط قطعی ناگهانی"""

    @patch("adb_utils.list_dir")
    @patch("adb_utils.adb_shell")
    @patch("adb_utils.path_exists")
    def test_stale_storage_rollback_restores_original_file(self, mock_exists, mock_shell, mock_list):
        # محتوای دایرکتوری موقت
        mock_list.return_value = ["txn_001.json"]

        def shell_side_effect(serial, cmd, **kwargs):
            res = MagicMock()
            res.returncode = 0

            # اولویت اول: خواندن فایل ژورنال
            if cmd.startswith("cat "):
                res.stdout = json.dumps({
                    "target": "/sdcard/Documents/notes.txt",
                    "rollback": "/sdcard/Documents/notes.txt.droidvault_rollback_12345",
                    "old_exists": True
                })
                return res

            # اولویت دوم: پیدا کردن پوشه موقت در اسکن اولیه دیسک
            if "NOW=$(date" in cmd or cmd.startswith("ROOT="):
                res.stdout = "/sdcard/.droidvault_tmp_12345\n"
                return res

            res.stdout = ""
            return res

        mock_shell.side_effect = shell_side_effect

        # فرض: فایل اصلی قطع/حذف شده، ولی فایل رول‌بک موجود است
        mock_exists.side_effect = lambda serial, p, **kw: "rollback" in p

        recovered = adbu.cleanup_stale_storage_temp("emulator-5554", "/sdcard")

        # بررسی اینکه فایل اصلی به مسیر درست بازگردانی شده است
        self.assertIn("/sdcard/Documents/notes.txt", recovered)


class TestAndroidSymlinkSecurity(unittest.TestCase):
    """تست ایمنی سیم‌لینک‌های خاص سیستم‌عامل اندروید"""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_accept_valid_android_lib_symlink(self):
        # سیم‌لینک مجاز اندروید به پوشه نصب اپلیکیشن
        valid_tar = os.path.join(self.test_dir.name, "app_with_lib.tar.gz")
        with tarfile.open(valid_tar, "w:gz") as tf:
            ti = tarfile.TarInfo(name="com.test.app/lib")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/data/app/~~xyz==/com.test.app-1/lib/arm64"
            tf.addfile(ti)

        ok, reason = adbu.validate_local_tar_archive(valid_tar, expected_top="com.test.app")
        self.assertTrue(ok, f"Valid lib symlink was wrongly rejected: {reason}")

    def test_reject_arbitrary_host_symlink(self):
        # تلاش برای ساخت سیم‌لینک به فایل‌های حساس لینوکسی/اندرویدی
        malicious_tar = os.path.join(self.test_dir.name, "malicious_symlink.tar.gz")
        with tarfile.open(malicious_tar, "w:gz") as tf:
            ti = tarfile.TarInfo(name="com.test.app/databases/leak")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/data/system/users/0/settings_secure.xml"
            tf.addfile(ti)

        ok, reason = adbu.validate_local_tar_archive(malicious_tar, expected_top="com.test.app")
        self.assertFalse(ok)
        self.assertIn("unsafe link target", reason)

if __name__ == "__main__":
    unittest.main()