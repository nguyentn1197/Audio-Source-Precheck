"""Fake hi-res detection via multi-signal spectrum analysis.

The detector combines several independent signals:
- cutoff ratio vs declared Nyquist
- brick-wall slope detection
- energy above 20 kHz
- post-cutoff flatness

The result is a structured verdict with evidence points rather than a single
boolean threshold.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

import numpy as np
import numpy.typing as npt
import soundfile as sf
from scipy.signal import welch

from musicflow.utils.logging_utils import get_logger

logger = get_logger(__name__)


class Verdict(StrEnum):
    GENUINE = "GENUINE"
    SUSPECT = "SUSPECT"
    LIKELY_FAKE = "LIKELY_FAKE"


@dataclass
class EvidencePoint:
    signal: str
    value: float
    interpretation: str


@dataclass
class HiResVerdict:
    verdict: Verdict
    confidence: float
    evidence: list[EvidencePoint] = field(default_factory=list)

    @property
    def label(self) -> str:
        prefix = {
            Verdict.GENUINE: "✓",
            Verdict.SUSPECT: "⚠",
            Verdict.LIKELY_FAKE: "✗",
        }[self.verdict]
        return f"{prefix} {self.verdict.value}"

    @property
    def summary(self) -> str:
        if not self.evidence:
            return self.label
        return " | ".join(point.interpretation for point in self.evidence)


@dataclass
class SpectrumResult:
    """Analysis result for a single audio file."""

    path: Path
    sample_rate: int
    bit_depth: int | None
    duration: float
    channels: int
    actual_cutoff_hz: float
    nyquist_hz: float
    is_suspect: bool
    confidence: float
    reason: str
    frequencies: npt.NDArray[np.float64] = field(repr=False, default_factory=lambda: np.array([]))
    power_db: npt.NDArray[np.float64] = field(repr=False, default_factory=lambda: np.array([]))
    spectrogram_times: npt.NDArray[np.float64] = field(repr=False, default_factory=lambda: np.array([]))
    spectrogram_freqs: npt.NDArray[np.float64] = field(repr=False, default_factory=lambda: np.array([]))
    spectrogram_db: npt.NDArray[np.float64] = field(repr=False, default_factory=lambda: np.zeros((0, 0)))
    hi_res_verdict: HiResVerdict | None = None

    @property
    def cutoff_ratio(self) -> float:
        if self.nyquist_hz <= 0:
            return 1.0
        return self.actual_cutoff_hz / self.nyquist_hz

    @property
    def status_label(self) -> str:
        if self.hi_res_verdict is not None:
            return self.hi_res_verdict.label
        if self.is_suspect:
            pct = int(self.cutoff_ratio * 100)
            return f"SUSPECT — energy only up to {pct}% of Nyquist ({self.actual_cutoff_hz:.0f} Hz)"
        return "OK"


def analyze_file(
    path: Path,
    threshold: float = 0.85,
    db_floor: float = -60.0,
    analysis_seconds: float = 30.0,
    cutoff_ratio_threshold: float = 0.50,
    slope_threshold_db_oct: float = 80.0,
) -> SpectrumResult:
    # `threshold` is retained for compatibility with existing worker/config wiring.
    try:
        return _analyze(
            path,
            threshold,
            db_floor,
            analysis_seconds,
            cutoff_ratio_threshold,
            slope_threshold_db_oct,
        )
    except Exception as exc:
        logger.warning("Spectrum analysis failed for %s: %s", path, exc)
        return SpectrumResult(
            path=path,
            sample_rate=0,
            bit_depth=None,
            duration=0.0,
            channels=0,
            actual_cutoff_hz=0.0,
            nyquist_hz=0.0,
            is_suspect=False,
            confidence=0.0,
            reason=f"Analysis error: {exc}",
        )


def _analyze(
    path: Path,
    threshold: float,
    db_floor: float,
    analysis_seconds: float,
    cutoff_ratio_threshold: float,
    slope_threshold_db_oct: float,
) -> SpectrumResult:
    # The new detector is driven by the derived signals below; `threshold` is legacy.
    samples, sample_rate = _load_audio(path, analysis_seconds)

    if samples.ndim > 1:
        samples = samples.mean(axis=1)

    n_samples = len(samples)
    duration = n_samples / sample_rate
    nyquist = sample_rate / 2.0

    nperseg = min(n_samples, 4096)
    freqs, psd = welch(samples, fs=sample_rate, nperseg=nperseg)
    psd_db = 10.0 * np.log10(np.maximum(psd, 1e-20))
    peak_db = float(psd_db.max())
    psd_db_rel = psd_db - peak_db

    significant = np.where(psd_db_rel >= db_floor)[0]
    actual_cutoff = float(freqs[significant[-1]]) if len(significant) else 0.0

    from scipy.signal import stft as scipy_stft  # noqa: PLC0415

    nperseg_stft = min(n_samples, 1024)
    stft_freqs, stft_times, Zxx = scipy_stft(
        samples, fs=sample_rate, nperseg=nperseg_stft, noverlap=nperseg_stft // 2
    )
    stft_power = np.abs(Zxx) ** 2
    stft_db = 10.0 * np.log10(np.maximum(stft_power, 1e-20))
    stft_peak = float(stft_db.max())
    stft_db_rel = stft_db - stft_peak

    bit_depth: int | None = None
    try:
        with path.open("rb") as fh:
            info = sf.info(fh)
        subtype = info.subtype
        if "16" in subtype:
            bit_depth = 16
        elif "24" in subtype:
            bit_depth = 24
        elif "32" in subtype:
            bit_depth = 32
    except Exception:
        pass

    evidence: list[EvidencePoint] = []
    cutoff_point = _signal_cutoff_ratio(freqs, psd_db_rel, nyquist, db_floor, cutoff_ratio_threshold)
    if cutoff_point is not None:
        evidence.append(cutoff_point)

    slope_point = _signal_brickwall_slope(freqs, psd_db_rel, db_floor, slope_threshold_db_oct)
    if slope_point is not None:
        evidence.append(slope_point)

    energy_point = _signal_above_20k_ratio(freqs, psd, sample_rate)
    if energy_point is not None:
        evidence.append(energy_point)

    flatness_point = _signal_post_cutoff_flatness(freqs, psd_db_rel, db_floor)
    if flatness_point is not None:
        evidence.append(flatness_point)

    hi_res_verdict = _build_verdict(evidence)
    is_suspect = hi_res_verdict.verdict != Verdict.GENUINE
    confidence = hi_res_verdict.confidence

    if evidence:
        reason = hi_res_verdict.summary
    else:
        reason = _build_reason(sample_rate, bit_depth, actual_cutoff, nyquist, False, threshold)

    logger.debug(
        "%s: sr=%d, cutoff=%.0fHz, nyquist=%.0fHz, suspect=%s, verdict=%s",
        path.name,
        sample_rate,
        actual_cutoff,
        nyquist,
        is_suspect,
        hi_res_verdict.verdict.value,
    )

    return SpectrumResult(
        path=path,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        duration=duration,
        channels=1,
        actual_cutoff_hz=actual_cutoff,
        nyquist_hz=nyquist,
        is_suspect=is_suspect,
        confidence=confidence,
        reason=reason,
        frequencies=freqs,
        power_db=psd_db_rel,
        spectrogram_times=stft_times,
        spectrogram_freqs=stft_freqs,
        spectrogram_db=stft_db_rel,
        hi_res_verdict=hi_res_verdict,
    )


def _load_audio(path: Path, max_seconds: float) -> tuple[npt.NDArray[np.float32], int]:
    ext = path.suffix.lower()
    if ext in {".flac", ".wav", ".aiff", ".aif", ".ogg", ".opus"}:
        try:
            with path.open("rb") as fh:
                with sf.SoundFile(fh) as f:
                    max_frames = int(max_seconds * f.samplerate)
                    samples = f.read(max_frames, dtype="float32", always_2d=False)
                    return samples, f.samplerate
        except Exception:
            pass

    import librosa  # noqa: PLC0415

    samples, sr = librosa.load(str(path), sr=None, mono=False, duration=max_seconds)
    return samples, int(sr)


def _build_reason(
    sample_rate: int,
    bit_depth: int | None,
    cutoff: float,
    nyquist: float,
    is_suspect: bool,
    threshold: float,
) -> str:
    sr_khz = sample_rate / 1000
    depth_str = f"{bit_depth}bit" if bit_depth else "lossy"
    if is_suspect:
        return (
            f"File claims {sr_khz:.1f}kHz/{depth_str} but energy cuts off at "
            f"{cutoff:.0f}Hz ({cutoff / nyquist * 100:.0f}% of Nyquist). "
            f"Likely upsampled from lower quality source."
        )
    return f"Spectrum looks genuine — energy present up to {cutoff:.0f}Hz ({cutoff / nyquist * 100:.0f}% of Nyquist)."


def _signal_cutoff_ratio(
    freqs: npt.NDArray[np.float64],
    psd_db_rel: npt.NDArray[np.float64],
    nyquist: float,
    db_floor: float,
    threshold: float,
) -> EvidencePoint | None:
    significant = np.where(psd_db_rel >= db_floor)[0]
    actual_cutoff = float(freqs[significant[-1]]) if len(significant) else 0.0
    ratio = actual_cutoff / nyquist if nyquist > 0 else 1.0
    if ratio >= threshold:
        return None
    return EvidencePoint(
        signal="cutoff_ratio",
        value=ratio,
        interpretation=(
            f"Energy cuts off at {actual_cutoff:.0f} Hz ({ratio * 100:.0f}% of declared Nyquist {nyquist:.0f} Hz)"
        ),
    )


def _signal_brickwall_slope(
    freqs: npt.NDArray[np.float64],
    psd_db_rel: npt.NDArray[np.float64],
    db_floor: float,
    slope_threshold_db_oct: float = 80.0,
) -> EvidencePoint | None:
    significant = np.where(psd_db_rel >= db_floor)[0]
    if len(significant) < 4:
        return None
    cutoff_idx = int(significant[-1])
    cutoff_hz = float(freqs[cutoff_idx])
    if cutoff_hz <= 0:
        return None
    half_cutoff_hz = cutoff_hz / 2.0
    half_idx = int(np.searchsorted(freqs, half_cutoff_hz))
    if half_idx <= 0 or half_idx >= len(freqs):
        return None

    def avg_db(idx: int) -> float:
        lo = max(0, idx - 2)
        hi = min(len(psd_db_rel), idx + 3)
        return float(np.mean(psd_db_rel[lo:hi]))

    db_at_cutoff = avg_db(cutoff_idx)
    db_at_half = avg_db(half_idx)
    slope = db_at_half - db_at_cutoff
    if slope < slope_threshold_db_oct:
        return None
    return EvidencePoint(
        signal="brickwall_slope",
        value=slope,
        interpretation=(
            f"Brick-wall filter detected at {cutoff_hz:.0f} Hz — slope of {slope:.0f} dB/octave"
        ),
    )


def _signal_above_20k_ratio(
    freqs: npt.NDArray[np.float64],
    psd: npt.NDArray[np.float64],
    sample_rate: int,
    ratio_threshold: float = 0.001,
) -> EvidencePoint | None:
    if sample_rate <= 48000:
        return None
    total_energy = float(np.sum(psd))
    if total_energy <= 0:
        return None
    above_20k_energy = float(np.sum(psd[freqs > 20000.0]))
    ratio = above_20k_energy / total_energy
    if ratio >= ratio_threshold:
        return None
    return EvidencePoint(
        signal="above_20k_ratio",
        value=ratio,
        interpretation=(
            f"Only {ratio * 100:.3f}% of total energy is above 20 kHz despite a {sample_rate / 1000:.1f} kHz sample rate"
        ),
    )


def _signal_post_cutoff_flatness(
    freqs: npt.NDArray[np.float64],
    psd_db_rel: npt.NDArray[np.float64],
    db_floor: float,
    flatness_std_threshold: float = 2.0,
    level_threshold: float = -50.0,
) -> EvidencePoint | None:
    significant = np.where(psd_db_rel >= db_floor)[0]
    if len(significant) == 0:
        return None
    cutoff_idx = int(significant[-1])
    cutoff_hz = float(freqs[cutoff_idx])
    above_mask = freqs > cutoff_hz
    if int(np.sum(above_mask)) < 8:
        return None
    post_db = psd_db_rel[above_mask]
    std_dev = float(np.std(post_db))
    mean_level = float(np.mean(post_db))
    if std_dev < flatness_std_threshold and mean_level < level_threshold:
        return EvidencePoint(
            signal="post_cutoff_flatness",
            value=std_dev,
            interpretation=(
                f"Digital silence above {cutoff_hz:.0f} Hz — std dev {std_dev:.1f} dB, mean level {mean_level:.0f} dBFS"
            ),
        )
    return None


def _build_verdict(evidence: list[EvidencePoint]) -> HiResVerdict:
    if not evidence:
        return HiResVerdict(verdict=Verdict.GENUINE, confidence=0.0, evidence=[])
    if any(ep.signal == "brickwall_slope" and ep.value > 150.0 for ep in evidence):
        return HiResVerdict(verdict=Verdict.LIKELY_FAKE, confidence=1.0, evidence=evidence)
    if len(evidence) == 1:
        return HiResVerdict(verdict=Verdict.SUSPECT, confidence=0.5, evidence=evidence)
    confidence = min(1.0, 0.7 + (len(evidence) - 2) * 0.15)
    return HiResVerdict(verdict=Verdict.LIKELY_FAKE, confidence=confidence, evidence=evidence)


def analyze_batch(
    paths: list[Path],
    threshold: float = 0.85,
    db_floor: float = -60.0,
    analysis_seconds: float = 30.0,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[SpectrumResult]:
    results: list[SpectrumResult] = []
    total = len(paths)
    for idx, path in enumerate(paths):
        if progress_cb:
            progress_cb(idx, total, path.name)
        results.append(analyze_file(path, threshold, db_floor, analysis_seconds))
    if progress_cb:
        progress_cb(total, total, "Done")
    return results


def save_spectrum_npz(result: SpectrumResult, audio_path: Path) -> Path:
    npz_path = audio_path.with_stem(audio_path.stem + ".spectrum")
    verdict = result.hi_res_verdict.verdict.value if result.hi_res_verdict else ""
    evidence = [
        {"signal": item.signal, "value": item.value, "interpretation": item.interpretation}
        for item in (result.hi_res_verdict.evidence if result.hi_res_verdict else [])
    ]
    np.savez_compressed(
        npz_path,
        frequencies=result.frequencies,
        power_db=result.power_db,
        spectrogram_times=result.spectrogram_times,
        spectrogram_freqs=result.spectrogram_freqs,
        spectrogram_db=result.spectrogram_db,
        sample_rate=np.array(result.sample_rate),
        bit_depth=np.array(result.bit_depth) if result.bit_depth is not None else np.array(0),
        actual_cutoff_hz=np.array(result.actual_cutoff_hz),
        nyquist_hz=np.array(result.nyquist_hz),
        is_suspect=np.array(result.is_suspect),
        confidence=np.array(result.confidence),
        verdict=np.array(verdict),
        evidence_json=np.array(json.dumps(evidence)),
    )
    return npz_path


def load_spectrum_npz(audio_path: Path) -> SpectrumResult | None:
    npz_path = audio_path.with_stem(audio_path.stem + ".spectrum")
    if not npz_path.exists():
        return None
    try:
        data = np.load(npz_path, allow_pickle=False)
        bit_depth = int(data["bit_depth"])
        hi_res_verdict: HiResVerdict | None = None
        if "verdict" in data and str(data["verdict"]) in {v.value for v in Verdict}:
            evidence: list[EvidencePoint] = []
            try:
                raw = json.loads(str(data["evidence_json"])) if "evidence_json" in data else []
                evidence = [
                    EvidencePoint(
                        signal=item["signal"],
                        value=float(item["value"]),
                        interpretation=item["interpretation"],
                    )
                    for item in raw
                ]
            except Exception:
                evidence = []
            hi_res_verdict = HiResVerdict(
                verdict=Verdict(str(data["verdict"])),
                confidence=float(data["confidence"]),
                evidence=evidence,
            )
        return SpectrumResult(
            path=audio_path,
            sample_rate=int(data["sample_rate"]),
            bit_depth=bit_depth if bit_depth != 0 else None,
            duration=0.0,
            channels=0,
            actual_cutoff_hz=float(data["actual_cutoff_hz"]),
            nyquist_hz=float(data["nyquist_hz"]),
            is_suspect=bool(data["is_suspect"]),
            confidence=float(data["confidence"]),
            reason=str(data["verdict"]) if "verdict" in data else "",
            frequencies=data["frequencies"],
            power_db=data["power_db"],
            spectrogram_times=data["spectrogram_times"] if "spectrogram_times" in data else np.array([]),
            spectrogram_freqs=data["spectrogram_freqs"] if "spectrogram_freqs" in data else np.array([]),
            spectrogram_db=data["spectrogram_db"] if "spectrogram_db" in data else np.zeros((0, 0)),
            hi_res_verdict=hi_res_verdict,
        )
    except Exception:
        return None


from PySide6.QtCore import QThread, Signal  # noqa: E402


class FakeHiResWorker(QThread):
    file_analyzed = Signal(object)
    progress = Signal(int, int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(
        self,
        paths: list[Path],
        threshold: float = 0.85,
        db_floor: float = -60.0,
        analysis_seconds: float = 30.0,
    ) -> None:
        super().__init__()
        self._paths = paths
        self._threshold = threshold
        self._db_floor = db_floor
        self._analysis_seconds = analysis_seconds

    def run(self) -> None:
        try:
            total = len(self._paths)
            for idx, path in enumerate(self._paths):
                self.progress.emit(idx, total, path.name)
                result = analyze_file(
                    path,
                    threshold=self._threshold,
                    db_floor=self._db_floor,
                    analysis_seconds=self._analysis_seconds,
                )
                self.file_analyzed.emit(result)
            self.progress.emit(total, total, "Done")
            self.finished.emit()
        except Exception as exc:
            logger.exception("FakeHiResWorker crashed")
            self.error.emit(str(exc))
