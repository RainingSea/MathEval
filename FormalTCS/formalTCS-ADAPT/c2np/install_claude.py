"""Install the pinned native Claude Code runtime during the C2NP image build."""

import hashlib
import os
import sys
import urllib.request
from pathlib import Path


VERSION = "2.1.323"
RELEASES = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases"
PLATFORMS = {
    "amd64": ("linux-x64", "604d29a6869409106b6a1110bdadcc8b72e8654a5a865d23258f938a696e348e"),
    "arm64": ("linux-arm64", "452dddff140b5caf00a5410333a3d6db82c73ea2dec74b1541fae18e97d47dca"),
}


def install(arch, dest):
    if arch not in PLATFORMS:
        raise SystemExit(f"Unsupported Docker architecture: {arch}")
    platform, checksum = PLATFORMS[arch]
    temp = dest.with_name(dest.name + ".download")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(f"{RELEASES}/{VERSION}/{platform}/claude", timeout=120) as response, temp.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != checksum:
            raise SystemExit("Claude Code binary checksum mismatch")
        temp.chmod(0o755)
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]))
