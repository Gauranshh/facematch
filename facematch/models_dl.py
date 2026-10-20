"""Model downloads (resumable, with retries). Base models ~39 MB; optional 'Accurate' recognition model (ArcFace w600k_r50, ~175 MB, from InsightFace's buffalo_l pack)."""
import os
import urllib.request
import zipfile

from .core import DATA_MODELS, DET_MODEL, MODELS

URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
BASE = {
    DET_MODEL: ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
    MODELS["sface"]["file"]: ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def fetch(url, dst, progress=lambda done, total: None, retries=10, min_size=100_000):
    """Download `url` to `dst`, resuming after dropped connections; the file only appears once complete."""
    dst = os.fspath(dst)
    tmp = dst + ".part"
    have, total = (os.path.getsize(tmp) if os.path.exists(tmp) else 0), 0
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:
                    have = 0                                   # server ignored Range: start over
                total = have + int(r.headers.get("Content-Length", 0))
                with open(tmp, "ab" if have else "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        have += len(b)
                        progress(have, total)
            if total and have >= total:
                break
        except Exception:
            have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if attempt == retries - 1:
                raise
    if os.path.getsize(tmp) < min_size:
        raise OSError(f"download of {url} looks incomplete")
    os.replace(tmp, dst)
    return dst


def download_base(say=print):
    """Fetch whichever of the two small base models is missing."""
    for dst, url in BASE.items():
        if dst.exists() and dst.stat().st_size > 100_000:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        say(f"Downloading {dst.name} …")
        fetch(url, dst)


def download_accurate(progress=lambda done, total: None, retries=8):
    dst = MODELS["arcface"]["file"]
    if dst.exists() and dst.stat().st_size > 1_000_000:
        return dst
    DATA_MODELS.mkdir(parents=True, exist_ok=True)
    tmp = DATA_MODELS / "buffalo_l.zip.part"
    total = 0
    for attempt in range(retries):
        have = tmp.stat().st_size if tmp.exists() else 0
        req = urllib.request.Request(URL, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                total = have + int(r.headers.get("Content-Length", 0))
                if have and r.status != 206:      # server ignored Range: start over
                    have = 0
                with open(tmp, "ab" if have else "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        have += len(b)
                        progress(have, total)
            if total and have >= total:
                break
        except Exception:
            if attempt == retries - 1:
                raise
    with zipfile.ZipFile(tmp) as z:
        with z.open("w600k_r50.onnx") as src, open(str(dst) + ".part", "wb") as out:
            while True:
                b = src.read(1 << 20)
                if not b:
                    break
                out.write(b)
    os.replace(str(dst) + ".part", dst)
    tmp.unlink(missing_ok=True)
    return dst
