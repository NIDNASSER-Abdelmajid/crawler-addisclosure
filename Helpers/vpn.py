"""Proton VPN Helper for AdGraph Crawler.

Provides cross-platform support for Windows, Linux, and macOS:
- Automated installation (winget on Windows, apt/brew/pip on Linux/macOS)
- Interactive country selection shell
- Country code / full name resolution
- Connect, disconnect, and status management
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("ProtonVPN")

# Comprehensive ISO 3166-1 alpha-2 and Proton VPN supported countries mapping
PROTON_COUNTRIES: dict[str, str] = {
    "AL": "Albania",
    "DZ": "Algeria",
    "AD": "Andorra",
    "AR": "Argentina",
    "AM": "Armenia",
    "AU": "Australia",
    "AT": "Austria",
    "AZ": "Azerbaijan",
    "BS": "Bahamas",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BM": "Bermuda",
    "BO": "Bolivia",
    "BA": "Bosnia and Herzegovina",
    "BR": "Brazil",
    "BG": "Bulgaria",
    "KH": "Cambodia",
    "CA": "Canada",
    "CL": "Chile",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DK": "Denmark",
    "DO": "Dominican Republic",
    "EC": "Ecuador",
    "EG": "Egypt",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "GE": "Georgia",
    "DE": "Germany",
    "GH": "Ghana",
    "GR": "Greece",
    "GT": "Guatemala",
    "HK": "Hong Kong",
    "HU": "Hungary",
    "IS": "Iceland",
    "IN": "India",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IT": "Italy",
    "JM": "Jamaica",
    "JP": "Japan",
    "KZ": "Kazakhstan",
    "KE": "Kenya",
    "LV": "Latvia",
    "LB": "Lebanon",
    "LI": "Liechtenstein",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MY": "Malaysia",
    "MT": "Malta",
    "MX": "Mexico",
    "MD": "Moldova",
    "MC": "Monaco",
    "ME": "Montenegro",
    "MA": "Morocco",
    "MM": "Myanmar",
    "NP": "Nepal",
    "NL": "Netherlands",
    "NZ": "New Zealand",
    "NG": "Nigeria",
    "MK": "North Macedonia",
    "NO": "Norway",
    "PK": "Pakistan",
    "PA": "Panama",
    "PE": "Peru",
    "PH": "Philippines",
    "PL": "Poland",
    "PT": "Portugal",
    "PR": "Puerto Rico",
    "RO": "Romania",
    "RS": "Serbia",
    "SG": "Singapore",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ZA": "South Africa",
    "KR": "South Korea",
    "ES": "Spain",
    "LK": "Sri Lanka",
    "SE": "Sweden",
    "CH": "Switzerland",
    "TW": "Taiwan",
    "TH": "Thailand",
    "TT": "Trinidad and Tobago",
    "TR": "Turkey",
    "UA": "Ukraine",
    "AE": "United Arab Emirates",
    "GB": "United Kingdom",
    "US": "United States",
    "UY": "Uruguay",
    "VE": "Venezuela",
    "VN": "Vietnam",
}

ALIASES: dict[str, str] = {
    "UK": "GB",
    "USA": "US",
    "UAE": "AE",
    "CZECHIA": "CZ",
    "KOREA": "KR",
    "RUSSIA": "RU",
    "GREAT BRITAIN": "GB",
    "ENGLAND": "GB",
    "AMERICA": "US",
}


def resolve_country(country_input: str) -> Tuple[str, str]:
    """Resolve a country input (initials/code or full name) to (alpha2_code, full_name)."""
    clean = country_input.strip()
    if not clean:
        return "", ""

    upper = clean.upper()

    if upper in PROTON_COUNTRIES:
        return upper, PROTON_COUNTRIES[upper]

    if upper in ALIASES:
        code = ALIASES[upper]
        return code, PROTON_COUNTRIES.get(code, clean)

    lower = clean.lower()
    for code, name in PROTON_COUNTRIES.items():
        if name.lower() == lower:
            return code, name

    if len(clean) >= 4:
        for code, name in PROTON_COUNTRIES.items():
            if lower in name.lower():
                return code, name

    return upper, clean


class VPNError(Exception):
    """Base exception for VPN errors."""
    pass


class VPNNotFoundError(VPNError):
    """Raised when Proton VPN CLI binary is not found on the system."""
    pass


class VPNConnectionError(VPNError):
    """Raised when connecting or disconnecting VPN fails."""
    pass


class ProtonVPNHelper:
    """Cross-platform manager for Proton VPN on Windows, Linux, and macOS."""

    def __init__(self, logger_instance: Optional[logging.Logger] = None):
        self.logger = logger_instance or logging.getLogger("ProtonVPN")
        self.system = platform.system()
        self.cli_path = self.find_cli_binary()

    def find_cli_binary(self) -> Optional[str]:
        """Locate the Proton VPN CLI executable across OS-specific paths."""
        candidates = [
            "protonvpn-cli",
            "protonvpn",
            "protonvpn-cli.exe",
            "protonvpn.exe",
            "ProtonVPN.exe",
        ]

        # 1. Search system PATH
        for cand in candidates:
            found = shutil.which(cand)
            if found:
                return found

        # 2. Windows specific path search
        if self.system == "Windows":
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
            program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
            
            search_roots = [
                Path(program_files) / "Proton",
                Path(program_files_x86) / "Proton",
                Path(program_files) / "Proton Technologies",
                Path(program_files_x86) / "Proton Technologies",
                Path(local_appdata) / "Programs" / "Proton",
                Path(local_appdata) / "ProtonVPN",
                Path(local_appdata) / "Proton",
            ]

            for root in search_roots:
                if root.is_dir():
                    # Check for ProtonVPN.exe or protonvpn-cli.exe recursively
                    for pattern in ("**/ProtonVPN.exe", "**/protonvpn.exe", "**/protonvpn-cli.exe"):
                        for match in root.glob(pattern):
                            if match.is_file():
                                return str(match.resolve())

        # 3. Linux specific paths
        elif self.system == "Linux":
            custom_linux_paths = [
                Path("/usr/bin/protonvpn-cli"),
                Path("/usr/local/bin/protonvpn-cli"),
                Path("/usr/bin/protonvpn"),
                Path("/usr/local/bin/protonvpn"),
                Path.home() / ".local" / "bin" / "protonvpn-cli",
                Path.home() / ".local" / "bin" / "protonvpn",
            ]
            for p in custom_linux_paths:
                if p.is_file() and os.access(p, os.X_OK):
                    return str(p)

        # 4. macOS specific paths
        elif self.system == "Darwin":
            custom_mac_paths = [
                Path("/usr/local/bin/protonvpn-cli"),
                Path("/opt/homebrew/bin/protonvpn-cli"),
                Path.home() / ".local" / "bin" / "protonvpn-cli",
                Path("/Applications/ProtonVPN.app/Contents/MacOS/ProtonVPN"),
            ]
            for p in custom_mac_paths:
                if p.is_file() and os.access(p, os.X_OK):
                    return str(p)

        return None

    def is_available(self) -> bool:
        """Check if Proton VPN CLI binary is available."""
        self.cli_path = self.find_cli_binary()
        return self.cli_path is not None

    def prompt_install_if_missing(self) -> bool:
        """Prompt user to install Proton VPN automatically or manually if not found."""
        if self.is_available():
            return True

        print("\n" + "=" * 60)
        print("  [!] Proton VPN is not detected on this system.")
        print(f"      Operating System: {self.system} ({platform.machine()})")
        print("=" * 60)
        print("\nHow would you like to proceed?")
        print("  [1] Install Proton VPN automatically now (recommended)")
        print("  [2] Install manually later (terminate crawl)")

        try:
            choice = input("\nSelect an option [1/2] (default: 1): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.")
            sys.exit(0)

        if choice in ("", "1", "y", "yes"):
            success = self.install_proton_vpn()
            if success:
                self.cli_path = self.find_cli_binary()
                print(f"\n[OK] Proton VPN installed successfully at: {self.cli_path}")
                return True
            else:
                print("\n[ERR] Automatic installation could not complete.", file=sys.stderr)
                self.print_manual_instructions()
                sys.exit(1)
        else:
            print("\nManual installation selected.")
            self.print_manual_instructions()
            print("\nTerminating process. Please run again after installing Proton VPN.")
            sys.exit(0)

    def print_manual_instructions(self) -> None:
        """Display OS-specific manual installation instructions."""
        print("\n" + "-" * 60)
        print(f"MANUAL INSTALLATION INSTRUCTIONS ({self.system}):")
        print("-" * 60)
        if self.system == "Windows":
            print("  Option A (Windows Package Manager - Winget):")
            print("    winget install --id Proton.ProtonVPN -e")
            print("  Option B (Official Installer):")
            print("    Download & install from: https://protonvpn.com/download")
        elif self.system == "Linux":
            print("  Option A (Debian / Ubuntu official package):")
            print("    sudo apt update && sudo apt install -y protonvpn-cli")
            print("  Option B (Fedora / RHEL):")
            print("    sudo dnf install -y protonvpn-cli")
            print("  Option C (Pip for Linux):")
            print("    pip3 install protonvpn-cli")
        elif self.system == "Darwin":
            print("  Option A (Homebrew):")
            print("    brew install --cask protonvpn")
            print("  Option B (Homebrew CLI):")
            print("    brew install protonvpn-cli")
        print("-" * 60)

    def install_proton_vpn(self) -> bool:
        """Execute platform-specific automatic installer."""
        # 1. Windows installation
        if self.system == "Windows":
            # Check if winget is available
            winget_path = shutil.which("winget")
            if winget_path:
                print("\n[INFO] Installing Proton VPN via Windows Package Manager (winget)...")
                cmd = [
                    winget_path,
                    "install",
                    "--id",
                    "Proton.ProtonVPN",
                    "-e",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ]
                try:
                    res = subprocess.run(cmd, check=False)
                    if res.returncode == 0:
                        self.cli_path = self.find_cli_binary()
                        if self.cli_path:
                            return True
                except Exception as e:
                    self.logger.warning(f"Winget install encountered an issue: {e}")

            # Fallback for Windows: open official download site
            print("\n[INFO] Opening Proton VPN official download page...")
            webbrowser.open("https://protonvpn.com/download")
            print("Please complete the installer wizard, then press Enter to continue...")
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                return False
            self.cli_path = self.find_cli_binary()
            return self.is_available()

        # 2. Linux installation
        elif self.system == "Linux":
            if shutil.which("apt"):
                print("\n[INFO] Installing Proton VPN via apt...")
                res = subprocess.run(["sudo", "apt", "update"], check=False)
                res = subprocess.run(["sudo", "apt", "install", "-y", "protonvpn-cli"], check=False)
                if res.returncode == 0 and self.is_available():
                    return True

            if shutil.which("dnf"):
                print("\n[INFO] Installing Proton VPN via dnf...")
                res = subprocess.run(["sudo", "dnf", "install", "-y", "protonvpn-cli"], check=False)
                if res.returncode == 0 and self.is_available():
                    return True

            # Pip fallback on Linux (valid on Linux because Linux supports pwd)
            print("\n[INFO] Installing protonvpn-cli via pip on Linux...")
            res = subprocess.run([sys.executable, "-m", "pip", "install", "protonvpn-cli"], check=False)
            if res.returncode == 0:
                self.cli_path = self.find_cli_binary()
                return self.is_available()

        # 3. macOS installation
        elif self.system == "Darwin":
            if shutil.which("brew"):
                print("\n[INFO] Installing Proton VPN via Homebrew...")
                res = subprocess.run(["brew", "install", "protonvpn-cli"], check=False)
                if res.returncode == 0 and self.is_available():
                    return True
                res = subprocess.run(["brew", "install", "--cask", "protonvpn"], check=False)
                if res.returncode == 0 and self.is_available():
                    return True

            print("\n[INFO] Opening Proton VPN macOS download page...")
            webbrowser.open("https://protonvpn.com/download")
            print("Please complete the installation, then press Enter to continue...")
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                return False
            self.cli_path = self.find_cli_binary()
            return self.is_available()

        return False

    def interactive_country_selector(self) -> str:
        """Launch an interactive shell menu to search or select a Proton VPN country."""
        self.prompt_install_if_missing()

        popular_countries = [
            ("US", "United States"),
            ("GB", "United Kingdom"),
            ("DE", "Germany"),
            ("FR", "France"),
            ("NL", "Netherlands"),
            ("CH", "Switzerland"),
            ("JP", "Japan"),
            ("CA", "Canada"),
            ("AU", "Australia"),
            ("SE", "Sweden"),
        ]

        while True:
            print("\n" + "=" * 60)
            print("        PROTON VPN - COUNTRY SELECTION SHELL")
            print(f"        OS: {self.system}  |  CLI: {Path(self.cli_path or '').name or 'N/A'}")
            print("=" * 60)
            print("Popular Locations:")
            for idx, (code, name) in enumerate(popular_countries, start=1):
                print(f"  [{idx:<2}] {code:<3} - {name}")
            print("\nSpecial Options:")
            print("  [F ] Fastest Server")
            print("  [R ] Random Server")
            print("  [S ] Search all supported countries")
            print("  [L ] List all supported countries")
            print("  [Q ] Cancel & Quit")
            print("=" * 60)

            try:
                choice = input("\nEnter choice (number, code, search, or option): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nAborted by user.")
                sys.exit(0)

            if not choice:
                continue

            choice_upper = choice.upper()

            if choice_upper == "Q":
                print("Cancelled.")
                sys.exit(0)

            if choice_upper == "F":
                return "fastest"

            if choice_upper == "R":
                return "random"

            if choice.isdigit():
                num = int(choice)
                if 1 <= num <= len(popular_countries):
                    selected_code, selected_name = popular_countries[num - 1]
                    print(f"\nSelected: {selected_name} ({selected_code})")
                    return selected_code

            if choice_upper == "L":
                print(f"\n{'Code':<6} | {'Country Name'}")
                print("-" * 40)
                for code, name in sorted(PROTON_COUNTRIES.items()):
                    print(f"{code:<6} | {name}")
                continue

            if choice_upper == "S":
                try:
                    search_term = input("\nEnter country name or code to search: ").strip()
                except (KeyboardInterrupt, EOFError):
                    continue
                if not search_term:
                    continue
                matches = [
                    (c, n) for c, n in PROTON_COUNTRIES.items()
                    if search_term.lower() in n.lower() or search_term.upper() == c
                ]
                if not matches:
                    print(f"[!] No countries matching '{search_term}'.")
                    continue

                print(f"\nMatching Countries ({len(matches)}):")
                for m_idx, (m_code, m_name) in enumerate(matches, start=1):
                    print(f"  [{m_idx}] {m_code} - {m_name}")
                try:
                    sub_choice = input("\nSelect match number (or press Enter to back): ").strip()
                    if sub_choice.isdigit() and 1 <= int(sub_choice) <= len(matches):
                        sel_code, sel_name = matches[int(sub_choice) - 1]
                        print(f"\nSelected: {sel_name} ({sel_code})")
                        return sel_code
                except (KeyboardInterrupt, EOFError):
                    continue
                continue

            # Direct code or country name typed by user
            code, name = resolve_country(choice)
            if code:
                print(f"\nSelected: {name} ({code})")
                return code
            else:
                print(f"[!] Unrecognized country: '{choice}'. Please try again.")

    def get_public_ip(self) -> dict:
        """Query current public IP and location info."""
        endpoints = [
            "https://ipapi.co/json/",
            "https://api.ipify.org?format=json",
        ]
        for url in endpoints:
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
        return {}

    def connect(self, country_input: str, fast: bool = False, timeout: int = 35) -> bool:
        """Connect Proton VPN to a specified country or handle interactive selection."""
        self.prompt_install_if_missing()

        if country_input.lower() in ("interactive", "select", "prompt", "menu"):
            country_input = self.interactive_country_selector()

        is_win_exe = self.system == "Windows" and self.cli_path.lower().endswith("protonvpn.exe")

        candidate_commands = []

        if country_input.lower() == "fastest":
            fast = True
            target_display = "Fastest Server"
            if is_win_exe:
                candidate_commands = [
                    [self.cli_path, "connect", "-f"],
                    [self.cli_path, "connect", "--fastest"],
                ]
            else:
                candidate_commands = [
                    [self.cli_path, "c", "-f"],
                    [self.cli_path, "connect", "--fastest"],
                ]
        elif country_input.lower() == "random":
            target_display = "Random Server"
            if is_win_exe:
                candidate_commands = [
                    [self.cli_path, "connect", "-r"],
                    [self.cli_path, "connect", "--random"],
                ]
            else:
                candidate_commands = [
                    [self.cli_path, "c", "-r"],
                    [self.cli_path, "connect", "--random"],
                ]
        else:
            code, name = resolve_country(country_input)
            target_display = f"{name} ({code})" if name != code else code

            if is_win_exe:
                # Windows ProtonVPN.exe syntax
                candidate_commands = [
                    [self.cli_path, "connect", "-c", code],
                    [self.cli_path, "connect", "-c", name],
                    [self.cli_path, "connect", "--country", code],
                    [self.cli_path, "connect", "--country", name],
                    [self.cli_path, "connect", code],
                ]
                if fast:
                    candidate_commands.insert(0, [self.cli_path, "connect", "-f", "-c", code])
            else:
                # Linux / macOS protonvpn-cli syntax
                candidate_commands = [
                    [self.cli_path, "connect", "--country", code],
                    [self.cli_path, "connect", "--country", name],
                    [self.cli_path, "c", "--cc", code],
                    [self.cli_path, "c", "-c", code],
                    [self.cli_path, "connect", code],
                ]
                if fast:
                    candidate_commands.insert(0, [self.cli_path, "c", "-f", "--cc", code])

        self.logger.info(f"Connecting Proton VPN to: {target_display}")

        last_error = ""
        for cmd in candidate_commands:
            cmd_str = " ".join(cmd)
            self.logger.info(f"Executing: {cmd_str}")
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if result.returncode == 0:
                    out = result.stdout.strip()
                    if out:
                        self.logger.info(out)
                    self.logger.info(f"Successfully connected to Proton VPN in {target_display}.")
                    return True

                err = result.stderr.strip() or result.stdout.strip()
                last_error = f"{cmd_str} failed (exit {result.returncode}): {err}"
                self.logger.warning(last_error)
            except subprocess.TimeoutExpired:
                last_error = f"Command timed out after {timeout}s: {cmd_str}"
                self.logger.warning(last_error)
            except OSError as exc:
                last_error = f"Execution error for {cmd_str}: {exc}"
                self.logger.warning(last_error)

        raise VPNConnectionError(
            f"Failed to connect to Proton VPN for '{country_input}' ({target_display}). Last error: {last_error}"
        )

    def disconnect(self, timeout: int = 20) -> bool:
        """Disconnect active Proton VPN connection."""
        if not self.is_available():
            raise VPNNotFoundError("Proton VPN is not installed.")

        candidate_commands = [
            [self.cli_path, "disconnect"],
            [self.cli_path, "d"],
        ]

        for cmd in candidate_commands:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if result.returncode == 0:
                    self.logger.info("Proton VPN disconnected successfully.")
                    return True
            except Exception as exc:
                self.logger.warning(f"Disconnect command {' '.join(cmd)} failed: {exc}")

        return False

    def get_status(self, timeout: int = 15) -> dict:
        """Get current VPN connection status."""
        if not self.is_available():
            return {"available": False, "connected": False, "error": "Proton VPN not found"}

        candidate_commands = [
            [self.cli_path, "status"],
            [self.cli_path, "s"],
        ]

        for cmd in candidate_commands:
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if result.returncode == 0:
                    return {
                        "available": True,
                        "connected": True,
                        "raw_output": result.stdout.strip(),
                    }
            except Exception:
                pass

        return {"available": True, "connected": False}


def connect_vpn(country_input: str, logger_instance: Optional[logging.Logger] = None) -> bool:
    """Helper function to connect to Proton VPN by country code, full name, or interactive prompt."""
    helper = ProtonVPNHelper(logger_instance=logger_instance)
    return helper.connect(country_input)


def disconnect_vpn(logger_instance: Optional[logging.Logger] = None) -> bool:
    """Helper function to disconnect Proton VPN."""
    helper = ProtonVPNHelper(logger_instance=logger_instance)
    return helper.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="Cross-platform Proton VPN helper for Windows, Linux, and macOS.",
    )
    subparsers = parser.add_subparsers(dest="command", help="VPN commands")

    # Interactive shell subparser
    subparsers.add_parser("interactive", help="Open interactive country selection shell.")

    # Connect subparser
    conn_parser = subparsers.add_parser("connect", help="Connect to Proton VPN in a country.")
    conn_parser.add_argument(
        "country",
        nargs="?",
        default="interactive",
        help="Country initials (e.g. US, FR) or full name (e.g. 'United States'). Leave empty for interactive shell.",
    )
    conn_parser.add_argument("--fast", action="store_true", help="Connect to fastest server.")

    # Install subparser
    subparsers.add_parser("install", help="Install or check Proton VPN CLI installation.")

    # Disconnect subparser
    subparsers.add_parser("disconnect", help="Disconnect active Proton VPN connection.")

    # Status subparser
    subparsers.add_parser("status", help="Get current VPN connection status and IP.")

    # List countries subparser
    subparsers.add_parser("countries", help="List supported country codes and names.")

    args = parser.parse_args()

    helper = ProtonVPNHelper()

    if args.command in (None, "interactive"):
        selected = helper.interactive_country_selector()
        print(f"\nConnecting to: {selected}...")
        try:
            helper.connect(selected)
        except VPNError as e:
            print(f"Error: {e}", file=sys.stderr)

    elif args.command == "connect":
        try:
            helper.connect(args.country, fast=args.fast)
        except VPNError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "install":
        helper.prompt_install_if_missing()

    elif args.command == "disconnect":
        try:
            helper.disconnect()
            print("Disconnected from Proton VPN.")
        except VPNError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "status":
        print(f"OS: {helper.system} ({platform.machine()})")
        print(f"Proton Available: {helper.is_available()} (Path: {helper.cli_path})")
        status = helper.get_status()
        if "raw_output" in status:
            print("\nCLI Status Output:\n" + status["raw_output"])
        ip_info = helper.get_public_ip()
        if ip_info:
            print("\nCurrent Public IP Info:")
            for k, v in ip_info.items():
                print(f"  {k}: {v}")

    elif args.command == "countries":
        print(f"{'Code':<6} | {'Country Name'}")
        print("-" * 35)
        for code, name in sorted(PROTON_COUNTRIES.items()):
            print(f"{code:<6} | {name}")


if __name__ == "__main__":
    main()
