"""Install a pinned official Linux x64 Claude Code native binary."""
import base64
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

VERSION = '2.1.322'
URL = f'https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-{VERSION}.tgz'
SHA512 = '0DoaNAXLWCYYIGachWBCkTUsS8dOOgvk4R2fFKdrLejO9XlRTwaWUMD9ONdDmv0csdpwxj4qLUN6zWMaG2Sz6g=='

if __name__ == '__main__':
    archive = urllib.request.urlopen(URL, timeout=300).read()
    assert base64.b64encode(hashlib.sha512(archive).digest()).decode() == SHA512, 'Claude archive integrity mismatch'
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        candidates = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == 'claude']
        assert len(candidates) == 1, f'Unexpected binary inventory: {tar.getnames()}'
        target = Path('/usr/local/bin/claude')
        target.write_bytes(tar.extractfile(candidates[0]).read())
        target.chmod(0o755)
    print(f'Installed Claude Code {VERSION}', flush=True)
