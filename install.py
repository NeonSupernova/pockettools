"""pockettools bootstrap installer for Pythonista.

One-liner (paste into the Pythonista console):
    import requests; exec(requests.get('https://raw.githubusercontent.com/NeonSupernova/pockettools/main/install.py').text)

Downloads the current main branch into Documents/pockettools via the bundled
updater, so a fresh install records its version and auto-updates from then on.
Re-running is safe: code is replaced, data/ is untouched.
"""
import io
import os
import sys
import urllib.request
import zipfile

REPO = 'NeonSupernova/pockettools'
DEST = os.path.expanduser('~/Documents/pockettools')


def main():
    req = urllib.request.Request('https://codeload.github.com/%s/zip/refs/heads/main' % REPO,
                                 headers={'User-Agent': 'pockettools-installer'})
    with urllib.request.urlopen(req, timeout=20) as r:
        blob = r.read()
    # Bootstrap only ptlib so the real updater (validation, manifest, backup) does the install.
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for info in z.infolist():
            rel = '/'.join(info.filename.split('/')[1:])
            if rel.startswith('ptlib/') and rel.endswith('.py'):
                p = os.path.join(DEST, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, 'wb') as f:
                    f.write(z.read(info))
    if DEST not in sys.path:
        sys.path.insert(0, DEST)
    for m in [m for m in sys.modules if m == 'ptlib' or m.startswith('ptlib.')]:
        del sys.modules[m]  # drop any stale copies Pythonista kept loaded
    from ptlib import updater
    status, detail = updater.check(force=True)
    if status != 'updated':
        raise SystemExit('Install failed: %s' % detail)
    print('Installed pockettools %s to %s' % (detail, DEST))
    print('Swipe Pythonista away, reopen, run pockettools/launcher.py')


main()
