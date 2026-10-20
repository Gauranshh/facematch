"""Downloads the models. Default: the two small ones (~39 MB). Add --accurate for the optional ArcFace model (~175 MB)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from facematch.models_dl import download_accurate, download_base  # noqa: E402

download_base()
if "--accurate" in sys.argv:
    print("fetching accurate model (175 MB)…")
    print(download_accurate(lambda a, t: print(f"\r  {a >> 20} / {t >> 20} MB", end="", flush=True)))
print("done")
