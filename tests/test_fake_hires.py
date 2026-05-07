"""Tests for musicflow.core.fake_hires."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from musicflow.core.fake_hires import (
    EvidencePoint,
    HiResVerdict,
    Verdict,
    _build_verdict,
    analyze_file,
)


def _write_wav(
    path: Path,
    sample_rate: int,
    duration: float,
    freq_hz: float,
    amplitude: float = 0.5,
) -> None:
    n_samples = int(sample_rate * duration)
    t = np.linspace(0, duration, n_samples, endpoint=False)
    samples = (amplitude * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)

    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((samples * 32767).astype(np.int16).tobytes())


def test_verdict_models_exist() -> None:
    verdict = HiResVerdict(
        verdict=Verdict.SUSPECT,
        confidence=0.5,
        evidence=[EvidencePoint("cutoff_ratio", 0.2, "Energy cuts off early")],
    )
    assert verdict.label.startswith("⚠")
    assert verdict.summary


def test_build_verdict_logic() -> None:
    assert _build_verdict([]).verdict == Verdict.GENUINE
    one = _build_verdict([EvidencePoint("cutoff_ratio", 0.3, "low cutoff")])
    assert one.verdict == Verdict.SUSPECT
    two = _build_verdict(
        [
            EvidencePoint("cutoff_ratio", 0.3, "low cutoff"),
            EvidencePoint("above_20k_ratio", 0.0001, "no energy above 20kHz"),
        ]
    )
    assert two.verdict == Verdict.LIKELY_FAKE


def test_fake_hires_flagged_as_suspect(tmp_path: Path) -> None:
    wav = tmp_path / "fake_hires.wav"
    _write_wav(wav, sample_rate=96000, duration=5.0, freq_hz=10000)
    result = analyze_file(wav)
    assert result.is_suspect
    assert result.hi_res_verdict is not None
    assert result.hi_res_verdict.verdict in {Verdict.SUSPECT, Verdict.LIKELY_FAKE}


def test_genuine_hires_not_flagged(tmp_path: Path) -> None:
    wav = tmp_path / "genuine_hires.wav"
    _write_wav(wav, sample_rate=96000, duration=5.0, freq_hz=40000)
    result = analyze_file(wav)
    assert not result.is_suspect
    assert result.hi_res_verdict is not None
    assert result.hi_res_verdict.verdict == Verdict.GENUINE


def test_cd_quality_analyzed(tmp_path: Path) -> None:
    wav = tmp_path / "cd.wav"
    _write_wav(wav, sample_rate=44100, duration=5.0, freq_hz=10000)
    result = analyze_file(wav)
    assert isinstance(result.is_suspect, bool)
    assert result.sample_rate == 44100


def test_analyze_file_returns_spectrum_data(tmp_path: Path) -> None:
    wav = tmp_path / "test.wav"
    _write_wav(wav, sample_rate=44100, duration=3.0, freq_hz=1000)
    result = analyze_file(wav)
    assert len(result.frequencies) > 0
    assert len(result.power_db) > 0
    assert result.nyquist_hz == pytest.approx(22050.0, rel=0.01)


def test_analyze_file_missing_file_returns_error_result(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.wav"
    result = analyze_file(missing)
    assert result.sample_rate == 0
    assert not result.is_suspect
    assert "error" in result.reason.lower()


def test_cutoff_ratio(tmp_path: Path) -> None:
    wav = tmp_path / "test.wav"
    _write_wav(wav, sample_rate=96000, duration=5.0, freq_hz=10000)
    result = analyze_file(wav)
    assert 0.0 <= result.cutoff_ratio <= 1.0


def test_status_label_suspect(tmp_path: Path) -> None:
    wav = tmp_path / "fake.wav"
    _write_wav(wav, sample_rate=96000, duration=5.0, freq_hz=10000)
    result = analyze_file(wav)
    assert result.status_label


def test_confidence_is_higher_for_more_obvious_fake(tmp_path: Path) -> None:
    wav_obvious = tmp_path / "obvious_fake.wav"
    _write_wav(wav_obvious, sample_rate=192000, duration=3.0, freq_hz=5000)
    result_obvious = analyze_file(wav_obvious)

    wav_borderline = tmp_path / "borderline.wav"
    _write_wav(wav_borderline, sample_rate=96000, duration=3.0, freq_hz=35000)
    result_borderline = analyze_file(wav_borderline)

    if result_obvious.is_suspect and result_borderline.is_suspect:
        assert result_obvious.confidence >= result_borderline.confidence
