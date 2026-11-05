import os
import sys
import tempfile
from pathlib import Path

# The app reads its settings at import time, so point it at a throwaway folder first.
_TMP = tempfile.mkdtemp(prefix="gd-test-")
os.environ["DATA_DIR"] = _TMP
os.environ.setdefault("TZ", "America/Chicago")
os.environ["ALLOWED_HOSTS"] = "testserver,garmin.example.net"
os.environ["ALLOWED_ORIGINS"] = "https://garmin.example.net"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
