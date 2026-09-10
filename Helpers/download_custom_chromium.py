"""Chromium snapshot downloader and executable resolver for AdGraph crawler.

Downloads and extracts specific Chromium snapshot builds for Windows, Linux, and macOS.
Ensures the binary is ready for Playwright to launch.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

try:
    from tqdm import tqdm
    _TQDM_AVAILABLE = True
except ImportError:
    _TQDM_AVAILABLE = False


DEFAULT_BUILD_NUMBER = "1687106"
SNAPSHOT_BASE_URL = "https://commondatastorage.googleapis.com/chromium-browser-snapshots/"


class ChromiumDownloadError(Exception):
    """Raised when downloading or preparing Chromium fails."""
    pass


class ChromiumPlatformError(ChromiumDownloadError):
    """Raised when current OS/architecture is not supported."""
    pass


class ChromiumExtractError(ChromiumDownloadError):
    """Raised when extracting the Chromium archive fails."""
    pass


class ChromiumDownloader:
    """Manages downloading, caching, and locating custom Chromium snapshot binaries."""

    def __init__(
        self,
        revision: str = DEFAULT_BUILD_NUMBER,
        output_dir: Optional[str | Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger("ChromiumDownloader")
        self.system = platform.system()
        self.machine = platform.machine().lower()
        self.device_type, self.zip_prefix, self.rel_bin_path = self._detect_platform()
        
        if revision.lower() in ("latest", "last_change"):
            self.revision = self.get_latest_revision()
            self.logger.info(f"Resolved 'latest' revision to: {self.revision}")
        else:
            self.revision = str(revision).strip()

        # Workspace/project default browsers folder
        if output_dir is None:
            # Place in <workspace_root>/browsers/chromium_<device>_<rev>
            repo_root = Path(__file__).resolve().parent.parent
            self.install_dir = repo_root / "browsers" / f"chromium_{self.device_type}_{self.revision}"
        else:
            self.install_dir = Path(output_dir).resolve()

        self.zip_filename = f"{self.zip_prefix}.zip"
        self.download_url = (
            f"{SNAPSHOT_BASE_URL}{self.device_type}/{self.revision}/{self.zip_filename}"
        )

    def _detect_platform(self) -> tuple[str, str, Path]:
        """Detect OS and architecture to determine snapshot folder, zip name, and relative binary path.
        
        Returns:
            tuple: (device_type, zip_prefix, relative_binary_path)
        """
        if self.system == "Windows":
            is_64 = "64" in self.machine or sys.maxsize > 2**32
            if is_64:
                device_type = "Win_x64"
                zip_prefix = "chrome-win"
                rel_bin = Path("chrome-win") / "chrome.exe"
            else:
                device_type = "Win"
                zip_prefix = "chrome-win"
                rel_bin = Path("chrome-win") / "chrome.exe"
            return device_type, zip_prefix, rel_bin

        if self.system == "Linux":
            is_64 = "64" in self.machine or "x86_64" in self.machine
            if not is_64:
                raise ChromiumPlatformError(f"Unsupported Linux architecture: {self.machine}. Only 64-bit Linux is supported.")
            device_type = "Linux_x64"
            zip_prefix = "chrome-linux"
            rel_bin = Path("chrome-linux") / "chrome"
            return device_type, zip_prefix, rel_bin

        if self.system == "Darwin":
            is_arm = "arm" in self.machine or "aarch64" in self.machine
            if is_arm:
                device_type = "Mac_Arm"
            else:
                device_type = "Mac"
            zip_prefix = "chrome-mac"
            rel_bin = Path("chrome-mac") / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
            return device_type, zip_prefix, rel_bin

        raise ChromiumPlatformError(f"Unsupported operating system: {self.system}")

    def get_latest_revision(self) -> str:
        """Fetch the latest available build revision from Google Cloud Storage."""
        url = f"{SNAPSHOT_BASE_URL}{self.device_type}/LAST_CHANGE"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            rev = resp.text.strip()
            if not rev:
                raise ChromiumDownloadError(f"Empty response from {url}")
            return rev
        except requests.RequestException as e:
            raise ChromiumDownloadError(
                f"Failed to fetch latest revision from {url}: {e}"
            ) from e

    def get_executable_path(self) -> Path:
        """Return the expected path to the Chromium executable binary."""
        return self.install_dir / self.rel_bin_path

    def is_installed(self) -> bool:
        """Check if the Chromium executable already exists and is valid."""
        exe = self.get_executable_path()
        return exe.is_file() and exe.stat().st_size > 0

    def download(self, force: bool = False, chunk_size: int = 1024 * 1024) -> Path:
        """Download and extract Chromium snapshot if not already installed.
        
        Args:
            force: If True, re-downloads even if already installed.
            chunk_size: Chunk size in bytes for streaming download (default 1MB).
            
        Returns:
            Path: Absolute path to the Chromium executable.
        """
        executable_path = self.get_executable_path()
        if not force and self.is_installed():
            self.logger.info(f"Chromium build {self.revision} already installed at: {executable_path}")
            return executable_path

        self.install_dir.mkdir(parents=True, exist_ok=True)
        temp_zip = self.install_dir / f"{self.zip_filename}.tmp"

        self.logger.info(
            f"Downloading Chromium revision {self.revision} ({self.device_type}) from: {self.download_url}"
        )

        try:
            response = requests.get(self.download_url, stream=True, timeout=30)
            if response.status_code == 404:
                raise ChromiumDownloadError(
                    f"Chromium revision {self.revision} not found on server ({self.download_url}). "
                    f"Please check the revision number or use 'latest'."
                )
            response.raise_for_status()

            total_bytes = int(response.headers.get("content-length", 0))
            downloaded = 0

            use_tqdm = _TQDM_AVAILABLE and (
                not logging.getLogger().handlers or sys.stdout.isatty() or total_bytes > 0
            )

            if use_tqdm:
                pbar = tqdm(
                    total=total_bytes,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=f"Chromium r{self.revision}",
                )
            else:
                pbar = None

            with open(temp_zip, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if pbar:
                            pbar.update(len(chunk))

            if pbar:
                pbar.close()

            self.logger.info(f"Download complete ({downloaded / (1024*1024):.1f} MB). Extracting archive...")

            # Extract archive
            try:
                with zipfile.ZipFile(temp_zip, "r") as zip_ref:
                    zip_ref.extractall(self.install_dir)
            except Exception as exc:
                raise ChromiumExtractError(f"Failed to extract zip archive {temp_zip}: {exc}") from exc

        except requests.RequestException as e:
            if temp_zip.exists():
                temp_zip.unlink(missing_ok=True)
            raise ChromiumDownloadError(f"Network error while downloading Chromium from {self.download_url}: {e}") from e
        finally:
            # Clean up temporary zip file
            if temp_zip.exists():
                try:
                    temp_zip.unlink()
                except OSError:
                    pass

        # Make sure binary has execution permissions on POSIX systems
        if self.system != "Windows":
            self._set_posix_permissions()

        if not self.is_installed():
            raise ChromiumDownloadError(
                f"Chromium executable was not found at expected location after extraction: {executable_path}"
            )

        self.logger.info(f"Chromium successfully installed and ready at: {executable_path}")
        return executable_path

    def _set_posix_permissions(self) -> None:
        """Ensure all binary files in the extracted directory are executable on Linux / macOS."""
        exe = self.get_executable_path()
        if exe.exists():
            current_mode = os.stat(exe).st_mode
            os.chmod(exe, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # On macOS or Linux, ensure child binaries/helpers also have +x
        for root, _, files in os.walk(self.install_dir):
            for file_name in files:
                file_path = os.path.join(root, file_name)
                # If file looks like a binary without extension or .sh
                if "." not in file_name or file_name.endswith((".sh", ".so")):
                    try:
                        mode = os.stat(file_path).st_mode
                        os.chmod(file_path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    except OSError:
                        pass


def ensure_custom_chromium(
    revision: str = DEFAULT_BUILD_NUMBER,
    output_dir: Optional[str | Path] = None,
    force: bool = False,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Convenience function to ensure a Chromium snapshot is downloaded and ready.
    
    Args:
        revision: Snapshot build/revision number (default "1687106" or "latest").
        output_dir: Optional destination folder.
        force: If True, re-download even if already present.
        logger: Optional logger instance.
        
    Returns:
        str: Absolute path to the Chromium executable binary.
    """
    downloader = ChromiumDownloader(revision=revision, output_dir=output_dir, logger=logger)
    exe_path = downloader.download(force=force)
    return str(exe_path)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Download and prepare a custom Chromium snapshot binary for AdGraph crawling.",
    )
    parser.add_argument(
        "--revision",
        "-r",
        default=DEFAULT_BUILD_NUMBER,
        help=f"Chromium revision/build number to download, or 'latest' (default: {DEFAULT_BUILD_NUMBER}).",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Directory where Chromium will be extracted (default: browsers/chromium_<device>_<rev>).",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force re-download even if already installed.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check if already installed and print path, do not download.",
    )

    args = parser.parse_args()

    downloader = ChromiumDownloader(revision=args.revision, output_dir=args.output_dir)

    if args.check_only:
        exe = downloader.get_executable_path()
        if downloader.is_installed():
            print(f"INSTALLED: {exe}")
            sys.exit(0)
        else:
            print(f"NOT_INSTALLED: {exe}")
            sys.exit(1)

    try:
        exe_path = downloader.download(force=args.force)
        print(f"\nChromium binary ready: {exe_path}")
    except ChromiumDownloadError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
