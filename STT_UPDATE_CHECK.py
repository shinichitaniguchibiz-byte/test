from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


RUNTIME_PACKAGES = (
    "faster-whisper",
    "ctranslate2",
    "huggingface-hub",
)

MODEL_REPOSITORIES = {
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
}

MODEL_ALLOW_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
)


class UpdateCheckError(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            text=True,
            capture_output=capture_output,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exception:
        raise UpdateCheckError(
            f"Update check timed out after {timeout_seconds} seconds."
        ) from exception


def _read_installed_versions(script_directory: Path) -> dict[str, str]:
    package_names_json = json.dumps(RUNTIME_PACKAGES)
    code = (
        "import importlib.metadata as m, json; "
        f"names=json.loads({package_names_json!r}); "
        "result={}; "
        "[(result.__setitem__(name, m.version(name)) if name in "
        "{d.metadata['Name'] for d in m.distributions()} else None) for name in names]; "
        "print(json.dumps(result, sort_keys=True))"
    )
    completed = _run(
        [sys.executable, "-c", code],
        cwd=script_directory,
        timeout_seconds=60,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise UpdateCheckError(
            "Could not read installed package versions: "
            + completed.stderr.strip()
        )

    try:
        return json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exception:
        raise UpdateCheckError(
            "Installed package version output was invalid."
        ) from exception


def _upgrade_runtime_packages(script_directory: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        "--quiet",
        *RUNTIME_PACKAGES,
    ]
    completed = _run(
        command,
        cwd=script_directory,
        timeout_seconds=600,
        capture_output=True,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise UpdateCheckError(
            "Runtime package update check failed. " + details
        )


def _refresh_model_cache(
    script_directory: Path,
    model_alias: str,
) -> str:
    repository = MODEL_REPOSITORIES.get(model_alias)
    if repository is None:
        raise UpdateCheckError(
            f"No model repository mapping is configured for: {model_alias}"
        )

    repository_json = json.dumps(repository)
    patterns_json = json.dumps(MODEL_ALLOW_PATTERNS)
    code = (
        "from huggingface_hub import snapshot_download; "
        f"repo={repository_json}; patterns={patterns_json}; "
        "path=snapshot_download(repo_id=repo, allow_patterns=patterns); "
        "print(path)"
    )
    completed = _run(
        [sys.executable, "-c", code],
        cwd=script_directory,
        timeout_seconds=1800,
        capture_output=True,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise UpdateCheckError(
            "Model update check failed. " + details
        )

    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise UpdateCheckError("Model update check returned no cache path.")

    return output_lines[-1]


def check_and_update_runtime(
    *,
    script_directory: Path,
    model_alias: str,
) -> None:
    started_at = time.perf_counter()
    print("UPDATE_CHECK_START")
    print("UPDATE_POLICY=CHECK_AND_INSTALL_LATEST_EVERY_RUN")

    before = _read_installed_versions(script_directory)
    print(
        "INSTALLED_VERSIONS_BEFORE="
        + json.dumps(before, ensure_ascii=False, sort_keys=True)
    )

    package_started_at = time.perf_counter()
    _upgrade_runtime_packages(script_directory)
    after = _read_installed_versions(script_directory)
    package_elapsed = time.perf_counter() - package_started_at

    print(
        "INSTALLED_VERSIONS_AFTER="
        + json.dumps(after, ensure_ascii=False, sort_keys=True)
    )
    changed = {
        name: {"before": before.get(name), "after": after.get(name)}
        for name in RUNTIME_PACKAGES
        if before.get(name) != after.get(name)
    }
    print(
        "UPDATED_PACKAGES="
        + (json.dumps(changed, ensure_ascii=False, sort_keys=True) if changed else "NONE")
    )
    print(f"PACKAGE_UPDATE_CHECK_SECONDS={package_elapsed:.3f}")

    model_started_at = time.perf_counter()
    model_path = _refresh_model_cache(
        script_directory=script_directory,
        model_alias=model_alias,
    )
    model_elapsed = time.perf_counter() - model_started_at

    print(f"MODEL_ALIAS={model_alias}")
    print(f"MODEL_CACHE_PATH={model_path}")
    print(f"MODEL_UPDATE_CHECK_SECONDS={model_elapsed:.3f}")
    print(f"UPDATE_CHECK_TOTAL_SECONDS={time.perf_counter() - started_at:.3f}")
    print("UPDATE_CHECK_RESULT=PASS")
