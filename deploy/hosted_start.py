"""Container-only bootstrap: initialize mounted volume ownership, then run as appuser."""

import os
import sys
from pathlib import Path

data = Path("/app/data")
data.mkdir(exist_ok=True)
if os.getuid() == 0:
    os.chown(data, 1000, 1000)
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
os.execv(sys.executable, [sys.executable, "/app/main.py"])
