from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def ensure_stt_virtual_environment() -> None:
    script_directory = Path(__file__).resolve().parent
    expected_python = script_directory / ".venv-stt" / "Scripts" / "python.exe"
    current_python = Path(sys.executable).resolve()

    if not expected_python.is_file():
        raise FileNotFoundError(
            f"The transcription virtual environment was not found: {expected_python}"
        )

    if current_python == expected_python.resolve():
        return

    print("PYTHON_ENVIRONMENT_SWITCH")
    print(f"FROM={current_python}")
    print(f"TO={expected_python}")

    completed = subprocess.run(
        [str(expected_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(script_directory),
        check=False,
    )
    raise SystemExit(completed.returncode)


def build_non_overlapping_clips(
    audio_seconds: float,
    clip_seconds: float,
) -> list[dict[str, float]]:
    clips: list[dict[str, float]] = []
    start = 0.0

    while start < audio_seconds:
        safe_start = max(0.0, round(start, 3))
        safe_end = min(audio_seconds, round(start + clip_seconds, 3))
        safe_end = max(safe_start, safe_end)

        if safe_end <= safe_start:
            break

        clips.append({"start": safe_start, "end": safe_end})

        if safe_end >= audio_seconds:
            break

        start = safe_end

    return clips


def main() -> int:
    ensure_stt_virtual_environment()

    script_directory = Path(__file__).resolve().parent

    from STT_UPDATE_CHECK import check_and_update_runtime

    check_and_update_runtime(
        script_directory=script_directory,
        model_alias="turbo",
    )

    import TRANSCRIBE_B_CORE as core

    core.CLIP_OVERLAP_SECONDS = 0.0
    core.build_full_coverage_clips = build_non_overlapping_clips
    return core.run()


if __name__ == "__main__":
    raise SystemExit(main())
