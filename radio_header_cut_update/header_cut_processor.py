from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


STATUS_COMPLETED = 0
STATUS_PENDING_DOWNLOAD = 10
STATUS_DOWNLOADING = 20
STATUS_DOWNLOAD_FAILED = 30
STATUS_PENDING_CONVERSION = 40
STATUS_CONVERTING = 50
STATUS_CONVERSION_FAILED = 60

WEEKDAY_COLUMNS = {
    0: "header_cut_mon_seconds",
    1: "header_cut_tue_seconds",
    2: "header_cut_wed_seconds",
    3: "header_cut_thu_seconds",
    4: "header_cut_fri_seconds",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_root(start: Path, explicit_db: str | None) -> tuple[Path, Path]:
    if explicit_db:
        db = Path(explicit_db).expanduser().resolve()
        if not db.is_file():
            raise FileNotFoundError(db)
        return db.parent.parent, db

    candidates: list[Path] = []
    for base in (start, start.parent, Path.cwd()):
        for candidate in (
            base / "database" / "radio_catalog.db",
            base.parent / "database" / "radio_catalog.db",
        ):
            if candidate.is_file():
                candidates.append(candidate.resolve())
    unique = sorted(set(candidates))
    if len(unique) == 1:
        db = unique[0]
        return db.parent.parent, db
    if not unique:
        raise FileNotFoundError("database\\radio_catalog.db が見つかりません")
    raise RuntimeError("radio_catalog.db が複数見つかりました: " + "; ".join(map(str, unique)))


def find_executable(root: Path, name: str, env_name: str) -> Path | None:
    env_value = os.environ.get(env_name)
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.is_file():
            return candidate.resolve()

    found = shutil.which(name)
    if found:
        return Path(found).resolve()

    exe = name + (".exe" if os.name == "nt" else "")
    candidates = [
        root / exe,
        root / "bin" / exe,
        root / "tools" / exe,
        root / "tools" / "ffmpeg" / "bin" / exe,
        root / "ffmpeg" / "bin" / exe,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def parse_onair_weekday(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).weekday()
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text[:19], fmt).weekday()
        except ValueError:
            continue
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10].replace("/", "-"), "%Y-%m-%d").weekday()
        except ValueError:
            return None
    return None


def cut_seconds_for(row: sqlite3.Row) -> int:
    weekday = parse_onair_weekday(row["onair_at"])
    weekday_value = None
    if weekday in WEEKDAY_COLUMNS:
        weekday_value = row[WEEKDAY_COLUMNS[weekday]]
    if weekday_value is not None:
        return max(0, int(weekday_value))
    default_value = row["header_cut_default_seconds"]
    return max(0, int(default_value or 0))


def resolve_original(root: Path, row: sqlite3.Row) -> Path:
    program_id = str(row["program_id"])
    file_name = str(row["file_name"] or "").strip()
    directory_name = str(row["directory_name"] or "").strip()

    raw_paths: list[Path] = []
    original_text = str(row["original_file_path"] or "").strip()
    if original_text:
        raw_paths.append(Path(original_text))

    saved_text = str(row["saved_directory"] or "").strip()
    if saved_text and file_name:
        raw_paths.append(Path(saved_text) / file_name)

    if file_name:
        raw_paths.extend(
            [
                root / "audio" / program_id / file_name,
                root / "audio" / directory_name / file_name,
                root / "audio" / file_name,
            ]
        )

    checked: list[Path] = []
    for candidate in raw_paths:
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        checked.append(candidate)
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    raise FileNotFoundError(
        "元の音声ファイルが見つかりません: " + " | ".join(map(str, checked))
    )


def probe_duration(ffprobe: Path | None, path: Path) -> float | None:
    if ffprobe is None:
        return None
    result = subprocess.run(
        [
            str(ffprobe),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def ffmpeg_codec_args(suffix: str) -> list[str]:
    suffix = suffix.lower()
    if suffix in (".m4a", ".mp4", ".aac"):
        return ["-c:a", "aac", "-b:a", "192k"]
    if suffix == ".wav":
        return ["-c:a", "pcm_s16le"]
    if suffix == ".flac":
        return ["-c:a", "flac"]
    return ["-c:a", "libmp3lame", "-q:a", "2"]


def run_conversion(
    ffmpeg: Path,
    original: Path,
    temporary: Path,
    cut_seconds: int,
) -> None:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(original),
        "-ss", str(cut_seconds),
        "-map_metadata", "0",
        "-vn",
        *ffmpeg_codec_args(original.suffix),
        str(temporary),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "FFmpeg returned an error"
        raise RuntimeError(f"FFmpeg exit code {result.returncode}: {error}")


def update_status(
    conn: sqlite3.Connection,
    recording_id: str,
    status_code: int,
    description: str,
    **values: object,
) -> None:
    assignments = ["status_code=?", "status_description=?", "updated_at=datetime('now','localtime')"]
    parameters: list[object] = [status_code, description]
    for name, value in values.items():
        assignments.append(f'"{name}"=?')
        parameters.append(value)
    parameters.append(recording_id)
    conn.execute(
        f"UPDATE recordings SET {', '.join(assignments)} WHERE recording_id=?",
        parameters,
    )
    conn.commit()


def select_rows(conn: sqlite3.Connection, mode: str) -> list[sqlite3.Row]:
    if mode == "retry-failed":
        predicate = "r.status_code=60"
    else:
        predicate = """
            r.status_code=40
            OR (
                r.status_code NOT IN (0,50,60)
                AND r.download_status IN ('downloaded','validated')
            )
        """
    sql = f"""
        SELECT
            r.recording_id,
            r.download_status,
            r.status_code,
            r.file_name,
            r.saved_directory,
            r.original_file_path,
            r.converted_file_path,
            e.onair_at,
            p.program_id,
            p.program_name,
            p.directory_name,
            p.header_cut_default_seconds,
            p.header_cut_mon_seconds,
            p.header_cut_tue_seconds,
            p.header_cut_wed_seconds,
            p.header_cut_thu_seconds,
            p.header_cut_fri_seconds
        FROM recordings r
        JOIN episodes e ON e.recording_id=r.recording_id
        JOIN programs p ON p.program_id=e.program_id
        WHERE {predicate}
        ORDER BY e.onair_at, r.recording_id
    """
    return list(conn.execute(sql))


def process_one(
    conn: sqlite3.Connection,
    root: Path,
    ffmpeg: Path | None,
    ffprobe: Path | None,
    row: sqlite3.Row,
    log,
) -> bool:
    recording_id = str(row["recording_id"])
    cut_seconds = cut_seconds_for(row)
    temporary: Path | None = None
    try:
        original = resolve_original(root, row)
        output_dir = root / "audio2" / str(row["program_id"])
        output_dir.mkdir(parents=True, exist_ok=True)
        output_name = str(row["file_name"] or original.name).strip() or original.name
        output = output_dir / output_name
        suffix = output.suffix or original.suffix or ".mp3"
        temporary = output.with_name(output.stem + ".tmp" + suffix)
        if temporary.exists():
            temporary.unlink()

        update_status(
            conn,
            recording_id,
            STATUS_CONVERTING,
            f"先頭{cut_seconds}秒のカットを開始した",
            applied_cut_seconds=cut_seconds,
            original_file_path=str(original),
            conversion_started_at=now_text(),
            conversion_completed_at=None,
        )

        if cut_seconds == 0:
            shutil.copy2(original, temporary)
        else:
            if ffmpeg is None:
                raise FileNotFoundError("ffmpeg.exe が見つかりません")
            run_conversion(ffmpeg, original, temporary, cut_seconds)

        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise RuntimeError("加工後の一時ファイルが作成されていません")

        converted_duration = probe_duration(ffprobe, temporary)
        if ffprobe is not None and converted_duration is None:
            raise RuntimeError("ffprobeで加工後ファイルの音声時間を確認できません")

        temporary.replace(output)
        size = output.stat().st_size
        digest = sha256_file(output)
        update_status(
            conn,
            recording_id,
            STATUS_COMPLETED,
            f"ダウンロードと先頭{cut_seconds}秒のカットが正常に終了した",
            converted_file_path=str(output),
            converted_file_size_bytes=size,
            converted_duration_seconds=converted_duration,
            converted_sha256=digest,
            conversion_completed_at=now_text(),
            last_error="",
        )
        log.write(
            f"RESULT=PASS RECORDING_ID={recording_id} PROGRAM={row['program_name']} "
            f"CUT_SECONDS={cut_seconds} ORIGINAL={original} OUTPUT={output}\n"
        )
        log.flush()
        print(f"PASS {recording_id} cut={cut_seconds}s")
        return True
    except Exception as exc:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
        description = f"{type(exc).__name__}: {exc}"
        update_status(
            conn,
            recording_id,
            STATUS_CONVERSION_FAILED,
            description,
            conversion_completed_at=now_text(),
            last_error=description,
        )
        log.write(
            f"RESULT=FAIL RECORDING_ID={recording_id} PROGRAM={row['program_name']} "
            f"ERROR={description}\n"
        )
        log.flush()
        print(f"FAIL {recording_id}: {description}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("pending", "retry-failed"),
        default="pending",
    )
    parser.add_argument("--database")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    root, db_path = find_root(script_dir, args.database)
    ffmpeg = find_executable(root, "ffmpeg", "FFMPEG_PATH")
    ffprobe = find_executable(root, "ffprobe", "FFPROBE_PATH")

    log_dir = root / "log" / "runs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"header_cut_{args.mode}_{stamp}.log"

    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        rows = select_rows(conn, args.mode)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"STARTED_AT={now_text()}\n")
            log.write(f"MODE={args.mode}\n")
            log.write(f"DATABASE={db_path}\n")
            log.write(f"FFMPEG={ffmpeg}\n")
            log.write(f"FFPROBE={ffprobe}\n")
            log.write(f"TARGET_COUNT={len(rows)}\n")
            passed = 0
            failed = 0
            for row in rows:
                if process_one(conn, root, ffmpeg, ffprobe, row, log):
                    passed += 1
                else:
                    failed += 1
            log.write(f"PASSED={passed}\nFAILED={failed}\n")
            log.write(f"COMPLETED_AT={now_text()}\n")

        print(f"TARGET_COUNT={len(rows)}")
        print(f"PASSED={passed}")
        print(f"FAILED={failed}")
        print(f"LOG={log_path}")
        return 0 if failed == 0 else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
