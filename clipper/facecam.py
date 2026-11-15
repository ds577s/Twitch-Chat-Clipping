"""Pick which configured facecam box is in use for a given clip, by looking for a face.

Streamers move their facecam around (different scenes, duo layouts). streamers.yaml can
list several candidate boxes; for each clip we sample frames and choose the box that
most often contains a face. If no box shows a face (e.g. full-screen gameplay scene),
the clip falls back to the center layout.

Needs opencv-python-headless 4.x (pip install "opencv-python-headless<5").
"""

from __future__ import annotations

from pathlib import Path

SAMPLES = 12


def available() -> bool:
    try:
        import cv2  # noqa: F401
        return hasattr(cv2, "CascadeClassifier")
    except ImportError:
        return False


def pick(video: Path, candidates: list[dict], samples: int = SAMPLES) -> tuple[int | None, list[int]]:
    """Return (index of best candidate or None, hit counts per candidate)."""
    import cv2

    base = cv2.data.haarcascades
    frontal = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None, [0] * len(candidates)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration_ms = frames / fps * 1000 if frames > 0 else 30000

    hits = [0] * len(candidates)
    for i in range(samples):
        cap.set(cv2.CAP_PROP_POS_MSEC, duration_ms * (i + 0.5) / samples)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        for k, c in enumerate(candidates):
            f = gray.shape[0] / c["ref_height"]  # config coords -> this video's resolution
            x, y, w, h = (int(round(c[key] * f)) for key in ("x", "y", "w", "h"))
            crop = gray[max(0, y):y + h, max(0, x):x + w]
            if crop.size == 0:
                continue
            crop = cv2.equalizeHist(crop)
            m = max(24, crop.shape[0] // 5)
            found = len(frontal.detectMultiScale(crop, 1.1, 5, minSize=(m, m))) > 0
            if not found:
                found = (len(profile.detectMultiScale(crop, 1.1, 5, minSize=(m, m))) > 0
                         or len(profile.detectMultiScale(cv2.flip(crop, 1), 1.1, 5, minSize=(m, m))) > 0)
            hits[k] += found
    cap.release()

    best = max(range(len(candidates)), key=lambda k: hits[k])
    if hits[best] == 0:
        return None, hits
    return best, hits
