# tests/test_safety_and_rollback.py
import os
import io
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# اضافه کردن ریشه پروژه به مسیر ایمپورت
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import adb_utils as adbu


class TestSecurityAndPathValidation(unittest.TestCase):
    """تست اعتبارسنجی اسامی فایل‌ها در برابر حملات Path Traversal"""

    def test_safe_member_name_blocking_traversal(self):
        # بررسی جلوگیری از دسترسی به مسیرهای خارج از دایرکتوری
        self.assertFalse(adbu._safe_member_name("../evil.sh"))
        self.assertFalse(adbu._safe_member_name("dir/../../etc/passwd"))
        self.assertFalse(adbu._safe_member_name("/system/bin/sh"))
        self.assertFalse(adbu._safe_member_name("C:\\Windows\\System32\\calc.exe"))
        self.assertFalse(adbu._safe_member_name("payload\x00.png"))
        self.assertFalse(adbu._safe_member_name(""))

    def test_safe_member_name_expected_top(self):
        # بررسی تطابق دایرکتوری ریشه
        self.assertTrue(adbu._safe_member_name("com.myapp/databases/db.sqlite", expected_top="com.myapp"))
        self.assertFalse(adbu._safe_member_name("com.otherapp/data.txt", expected_top="com.myapp"))

    def test_safe_member_name_valid_cases(self):
        self.assertTrue(adbu._safe_member_name("files/cache/sample.dat"))
        self.assertTrue(adbu._safe_member_name("databases/main.db"))


class TestArchiveIntegrityValidation(unittest.TestCase):
    """تست ارزیابی سلامت و ایمنی ساختار آرشیو قبل از بازگردانی"""

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
    """تست شبیه‌سازی شکست و اطمینان از پاک‌سازی فایل‌های موقت (.part)"""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.test_dir.cleanup()

    @patch("subprocess.Popen")
    def test_stream_tar_pull_cleans_up_part_file_on_error(self, mock_popen):
        # شبیه‌سازی رخ دادن خطا و شکست در حین استریم
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


if __name__ == "__main__":
    unittest.main()