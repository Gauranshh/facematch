"""Face engine: YuNet (detection) + a recognition model (SFace "standard" or ArcFace "accurate"). CPU only, offline."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageOps

try:  # iPhone .heic photos
    import pillow_heif
    pillow_heif.register_heif_opener()
    _HEIC = True
except Exception:
    _HEIC = False

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models"
DATA_MODELS = Path(os.environ.get("FACEMATCH_DATA", Path.home() / ".facematch")).expanduser() / "models"
DET_MODEL = MODEL_DIR / "face_detection_yunet_2023mar.onnx"

IMAGE_EXTS = {".jpg", ".jpeg", ".jpe", ".png", ".bmp", ".webp", ".tif", ".tiff"} | ({".heic", ".heif"} if _HEIC else set())
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".3gp", ".webm"}
MIN_FACE_PX = 24

# thr = cosine similarity above which two faces are "the same person" (default slider position)
MODELS = {
    "sface": dict(label="Standard (fast)", dim=128, thr=0.363, file=MODEL_DIR / "face_recognition_sface_2021dec.onnx",
                  pen=0.30, assign=0.43, form=0.47, merge=0.58, member=0.40),
    "arcface": dict(label="Accurate (slower; better with children, profiles, ageing)", dim=512, thr=0.35,
                    file=DATA_MODELS / "w600k_r50.onnx", pen=0.25, assign=0.42, form=0.46, merge=0.58, member=0.37),
}
STORE_F16_ABOVE = 256   # embeddings with more dims than this are stored as float16


def encode_emb(e: np.ndarray, dim: int) -> bytes:
    return e.astype(np.float16 if dim > STORE_F16_ABOVE else np.float32).tobytes()


def decode_embs(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16 if dim > STORE_F16_ABOVE else np.float32).reshape(-1, dim)


def model_available(name: str) -> bool:
    f = MODELS[name]["file"]
    return f.exists() and f.stat().st_size > 1_000_000


def load_image(path) -> Optional[np.ndarray]:
    """Load as BGR uint8, honouring EXIF rotation. None if unreadable."""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


@dataclass
class Face:
    box: tuple      # x, y, w, h in the coordinates of the image that was passed in
    emb: np.ndarray  # L2-normalised float32
    det: float
    q: float        # quality 0..1 (size + sharpness)


class _SFace:
    def __init__(self, path):
        self.rec = cv2.FaceRecognizerSF.create(str(path), "")

    def __call__(self, img, row):
        al = self.rec.alignCrop(img, row)
        return al, self.rec.feature(al).flatten()


class _ArcFace:
    TPL = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)

    def __init__(self, path):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        self.s = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        self.inp = self.s.get_inputs()[0].name

    def __call__(self, img, row):
        lm = np.asarray(row[4:14], np.float32).reshape(5, 2)
        M, _ = cv2.estimateAffinePartial2D(lm, self.TPL, method=cv2.LMEDS)
        if M is None:
            M = cv2.getAffineTransform(lm[:3], self.TPL[:3])
        al = cv2.warpAffine(img, M, (112, 112), borderValue=0)
        blob = ((al[:, :, ::-1].astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
        return al, self.s.run(None, {self.inp: blob})[0][0]


class FaceMatcher:
    def __init__(self, model: str = "sface"):
        if not DET_MODEL.exists() or DET_MODEL.stat().st_size < 100_000:
            raise FileNotFoundError(f"Model missing: {DET_MODEL}. Run: python download_models.py")
        if not model_available(model):
            raise FileNotFoundError(f"Recognition model '{model}' is not downloaded yet ({MODELS[model]['file']}).")
        self.model = model
        self.dim = MODELS[model]["dim"]
        self._det = cv2.FaceDetectorYN.create(str(DET_MODEL), "", (320, 320), 0.8, 0.3, 5000)
        self._emb = _SFace(MODELS[model]["file"]) if model == "sface" else _ArcFace(MODELS[model]["file"])
        self._lock = threading.Lock()  # OpenCV DNN objects aren't thread-safe

    def embed_faces(self, img: np.ndarray, max_side: int = 1280) -> list[Face]:
        """Detect + embed every face. Returned boxes are in `img` coordinates."""
        h, w = img.shape[:2]
        scale = min(1.0, max_side / max(h, w))
        small = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA) if scale < 1 else img
        sh, sw = small.shape[:2]
        out = []
        with self._lock:
            self._det.setInputSize((sw, sh))
            _, faces = self._det.detect(small)
            if faces is None:
                return out
            for f in faces:
                fw, fh = float(f[2]), float(f[3])
                if min(fw, fh) < MIN_FACE_PX:
                    continue
                al, feat = self._emb(small, f)
                feat = feat.astype(np.float32).flatten()
                feat /= (np.linalg.norm(feat) + 1e-9)
                lv = cv2.Laplacian(cv2.cvtColor(al, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                # lv: sharp faces ~800-1900, soft ~200, motion-blurred ~100, very blurry <60
                q = min(1.0, min(fw, fh) / 90.0) ** 0.6 * (0.2 + 0.8 * min(1.0, lv / 600.0))
                box = tuple(int(round(v / scale)) for v in (f[0], f[1], fw, fh))
                out.append(Face(box, feat, float(f[-1]), float(q)))
        return out
