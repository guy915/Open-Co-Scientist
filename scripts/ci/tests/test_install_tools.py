import hashlib
import io
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.ci.install_tools import extract_verified


class InstallToolsTests(unittest.TestCase):
    def archive(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            member = tarfile.TarInfo("gitleaks")
            member.size = 6
            archive.addfile(member, io.BytesIO(b"binary"))
        return data.getvalue()

    def zip_archive(self, mode=stat.S_IFREG):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            member = zipfile.ZipInfo("bun-linux-x64/bun")
            member.external_attr = (mode | 0o755) << 16
            archive.writestr(member, b"binary")
            archive.writestr("unrelated", b"unused")
        return data.getvalue()

    def test_checksum_mismatch_fails_before_writing_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gitleaks"
            for archive in (self.archive(), self.zip_archive()):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    extract_verified(archive, "0" * 64, "gitleaks", path)
            self.assertFalse(path.exists())

    def test_verified_binary_alone_is_extracted_and_executable(self):
        archive = self.archive()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gitleaks"
            extract_verified(
                archive, hashlib.sha256(archive).hexdigest(), "gitleaks", path
            )
            self.assertEqual(path.read_bytes(), b"binary")
            self.assertTrue(path.stat().st_mode & 0o111)

    def test_verified_zip_binary_alone_is_extracted_and_executable(self):
        archive = self.zip_archive()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bin/bun"
            extract_verified(
                archive, hashlib.sha256(archive).hexdigest(), "bun-linux-x64/bun", path
            )
            self.assertEqual(path.read_bytes(), b"binary")
            self.assertTrue(path.stat().st_mode & 0o111)
            self.assertEqual(
                [item.relative_to(root) for item in root.rglob("*") if item.is_file()],
                [Path("bin/bun")],
            )

    def test_zip_symlinks_directories_and_devices_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bun"
            for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFCHR):
                with self.subTest(mode=mode):
                    archive = self.zip_archive(mode)
                    with self.assertRaisesRegex(ValueError, "regular"):
                        extract_verified(
                            archive,
                            hashlib.sha256(archive).hexdigest(),
                            "bun-linux-x64/bun",
                            path,
                        )
                    self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
