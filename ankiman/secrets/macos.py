from __future__ import annotations

import subprocess

SERVICE = "ankiman"


class MacOSKeychainStore:
    def get(self, key: str) -> str | None:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", SERVICE, "-a", key, "-w"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return None
        value = result.stdout.strip()
        return value or None

    def set(self, key: str, value: str) -> None:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                SERVICE,
                "-a",
                key,
                "-w",
                value,
                "-U",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

    def delete(self, key: str) -> bool:
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", SERVICE, "-a", key],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return False
        return True
