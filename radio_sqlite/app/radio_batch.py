from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


VERSION = "20260726_sqlite_headless_v1"
JST = timezone(timedelta(hours=9), name="JST")
ROOT = Path(__file__).resolve().parents[1]
DATABASE_DIR = ROOT / "database"
DATABASE_PATH = DATABASE_DIR / "radio.db"
SCHEMA_PATH = DATABASE_DIR / "schema.sql"
SEED_PATH = DATABASE_DIR / "seed_programs.sql"
LOG_DIR = ROOT / "log" / "runs"
WORK_DIR = ROOT / ".nhk_download_work"
LOCK_PATH = ROOT / ".radio_sqlite.lock"
DEFAULT_FFMPEG = Path(r"C:\OneDrive2\OneDrive\lib\bin\ffmpeg.exe")
NHK_API = "https://www.nhk.or.jp/radio-api/app/v1/web/ondemand/series"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0 Safari/537.36"
)


class RadioError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunContext:
    run_id: str
    command_name: str
    started_at: str
    log_path: Path
    logger: logging.Logger


class JstFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, JST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S JST")


class ProcessLock:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.fd: int | None = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self.fd = os.open(str(self.path), flags)
        except FileExistsError as exc:
            detail = ""
            with contextlib.suppress(OSError):
                detail = self.path.read_text(encoding="utf-8", errors="replace")
            raise RadioError(
                f"Another Radio batch is already running. Lock={self.path} {detail}"
            ) from exc
        payload = {
            "run_id": self.run_id,
            "pid": os.getpid(),
            "created_at": now_iso(),
            "version": VERSION,
        }
        os.write(self.fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        os.fsync(self.fd)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
        with contextlib.suppress(OSError):
            self.path.unlink()


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def new_run_id() -> str:
    return datetime.now(JST).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


def ensure_directories() -> None:
    for path in (DATABASE_DIR, LOG_DIR, WORK_DIR, ROOT / "audio"):
        path.mkdir(parents=True, exist_ok=True)


def create_logger(run_id: str) -> tuple[logging.Logger, Path]:
    ensure_directories()
    log_path = LOG_DIR / f"{run_id}.log"
    logger = logging.getLogger(f"radio.{run_id}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = JstFormatter(
        "%(asctime)s [%(levelname)s] [run=" + run_id + "] %(message)s"
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger, log_path


def connect_db(path: Path = DATABASE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize_database(path: Path = DATABASE_PATH, seed: bool = True) -> None:
    if not SCHEMA_PATH.exists():
        raise RadioError(f"Schema file is missing: {SCHEMA_PATH}")
    schema = SCHEMA_PATH.read_text(encoding="utf-8-sig")
    with connect_db(path) as conn:
        conn.executescript(schema)
        if seed:
            if not SEED_PATH.exists():
                raise RadioError(f"Seed file is missing: {SEED_PATH}")
            conn.executescript(SEED_PATH.read_text(encoding="utf-8-sig"))
        conn.commit()


def relative_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def start_run(conn: sqlite3.Connection, ctx: RunContext) -> None:
    conn.execute(
        """
        INSERT INTO run(run_id, command_name, started_at, status, log_path)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (ctx.run_id, ctx.command_name, ctx.started_at, relative_to_root(ctx.log_path)),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    ctx: RunContext,
    *,
    status: str,
    exit_code: int,
    program_count: int = 0,
    planned_count: int = 0,
    success_count: int = 0,
    skip_count: int = 0,
    error_count: int = 0,
    description: str | None = None,
    validation_zip_path: Path | None = None,
) -> None:
    conn.execute(
        """
        UPDATE run
           SET ended_at = ?, status = ?, exit_code = ?,
               program_count = ?, planned_count = ?, success_count = ?,
               skip_count = ?, error_count = ?, description = ?,
               validation_zip_path = ?
         WHERE run_id = ?
        """,
        (
            now_iso(),
            status,
            exit_code,
            program_count,
            planned_count,
            success_count,
            skip_count,
            error_count,
            description,
            relative_to_root(validation_zip_path) if validation_zip_path else None,
            ctx.run_id,
        ),
    )
    conn.commit()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_status(value: Any) -> str:
    text = normalize_text(value).lower()
    mapping = {
        "": "pending",
        "0": "pending",
        "0.0": "pending",
        "1": "downloaded",
        "1.0": "downloaded",
        "pending": "pending",
        "planned": "pending",
        "downloading": "downloading",
        "downloaded": "downloaded",
        "success": "downloaded",
        "complete": "downloaded",
        "completed": "downloaded",
        "skip": "skipped",
        "skipped": "skipped",
        "error": "error",
        "failed": "error",
    }
    return mapping.get(text, "pending")


def extract_site_corner(program_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(program_url)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("p"):
        token = query["p"][0]
        match = re.fullmatch(r"([A-Za-z0-9]+)(?:_([A-Za-z0-9]+))?", token)
        if match:
            return match.group(1), match.group(2) or "01"
    match = re.search(r"/rs/([A-Za-z0-9]+)/", parsed.path)
    if match:
        return match.group(1), "01"
    raise RadioError(f"Cannot determine site_id from program_url: {program_url}")


def selected_programs(
    conn: sqlite3.Connection,
    program_selector: str | None,
    active_only: bool = True,
) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if active_only:
        where.append("is_active = 1")
    if program_selector:
        where.append(
            "(CAST(program_id AS TEXT) = ? OR upper(program_abbreviation) = upper(?) "
            "OR program_name = ?)"
        )
        params.extend([program_selector, program_selector, program_selector])
    sql = "SELECT * FROM program"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY program_id"
    rows = list(conn.execute(sql, params))
    if program_selector and not rows:
        raise RadioError(f"Program was not found: {program_selector}")
    return rows


def http_json(url: str, referer: str, logger: logging.Logger) -> Mapping[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
                "Referer": referer,
            },
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                elapsed = time.monotonic() - started
                logger.debug(
                    "NHK_RESPONSE status=%s bytes=%s elapsed=%.3fs url=%s",
                    getattr(response, "status", None),
                    len(body),
                    elapsed,
                    url,
                )
                data = json.loads(body.decode("utf-8-sig"))
                if not isinstance(data, dict):
                    raise RadioError(f"NHK response is not an object: {type(data).__name__}")
                return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RadioError) as exc:
            last_error = exc
            logger.warning("NHK request attempt %s failed: %s", attempt, exc)
            if attempt < 3:
                time.sleep(attempt * 2)
    raise RadioError(f"NHK request failed after 3 attempts: {last_error}")


def parse_iso_from_contents_id(value: str) -> tuple[datetime | None, datetime | None]:
    matches = re.findall(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)",
        value,
    )
    parsed: list[datetime] = []
    for item in matches[:2]:
        with contextlib.suppress(ValueError):
            parsed.append(datetime.fromisoformat(item.replace("Z", "+00:00")))
    if not parsed:
        return None, None
    return parsed[0], parsed[1] if len(parsed) > 1 else None


def parse_japanese_onair(value: str) -> datetime | None:
    match = re.search(
        r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日.*?(午前|午後)(\d{1,2}):(\d{2})",
        value,
    )
    if not match:
        return None
    current = datetime.now(JST)
    year = int(match.group(1)) if match.group(1) else current.year
    month = int(match.group(2))
    day = int(match.group(3))
    ampm = match.group(4)
    hour = int(match.group(5))
    minute = int(match.group(6))
    if ampm == "午前" and hour == 12:
        hour = 0
    elif ampm == "午後" and hour != 12:
        hour += 12
    try:
        candidate = datetime(year, month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None
    if not match.group(1):
        if candidate - current > timedelta(days=180):
            candidate = candidate.replace(year=year - 1)
        elif current - candidate > timedelta(days=300):
            candidate = candidate.replace(year=year + 1)
    return candidate


def clean_episode_subtitle(series_title: str, episode_title: str) -> str:
    text = episode_title.strip()
    if series_title and text.startswith(series_title):
        text = text[len(series_title) :].strip()
    text = text.strip("　 \t\r\n-–—:：")
    if text.startswith("「") and text.endswith("」") and len(text) >= 2:
        text = text[1:-1].strip()
    return text or episode_title.strip()


def safe_filename(value: str, max_length: int = 120) -> str:
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if not text:
        text = "untitled"
    return text[:max_length].rstrip(". ")


def resolve_output_root(program: Mapping[str, Any]) -> Path:
    raw = Path(str(program["output_directory"]))
    if not raw.is_absolute():
        raw = ROOT / raw
    return raw


def make_relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path.resolve())


def metadata_rows_for_program(
    program: Mapping[str, Any], data: Mapping[str, Any]
) -> list[dict[str, Any]]:
    site_id = normalize_text(program["site_id"])
    corner_id = normalize_text(program["corner_id"]) or "01"
    series_title = normalize_text(data.get("title")) or normalize_text(program["program_name"])
    episodes = data.get("episodes") or []
    if not isinstance(episodes, list):
        raise RadioError(f"episodes is not a list for {program['program_name']}")
    output_dir = resolve_output_root(program) / normalize_text(program["directory_name"])
    rows: list[dict[str, Any]] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        episode_id = normalize_text(episode.get("id"))
        stream_url = normalize_text(episode.get("stream_url"))
        episode_title = normalize_text(episode.get("program_title")) or series_title
        if not episode_id or not stream_url:
            continue
        recording_id = f"{site_id}_{corner_id}_{episode_id}"
        aa_contents_id = normalize_text(episode.get("aa_contents_id"))
        start_at, end_at = parse_iso_from_contents_id(aa_contents_id)
        if start_at is None:
            start_at = parse_japanese_onair(normalize_text(episode.get("onair_date")))
        expected = None
        if start_at is not None and end_at is not None:
            expected = max(0.0, (end_at - start_at).total_seconds())
        title_sub = clean_episode_subtitle(series_title, episode_title)
        date_prefix = (
            start_at.astimezone(JST).strftime("%y%m%d")
            if start_at is not None
            else datetime.now(JST).strftime("%y%m%d")
        )
        file_name = f"{date_prefix}_{safe_filename(title_sub)}.m4a"
        final_path = output_dir / file_name
        rows.append(
            {
                "recording_id": recording_id,
                "program_id": int(program["program_id"]),
                "program_name": normalize_text(program["program_name"]),
                "program_abbreviation": normalize_text(program["program_abbreviation"]),
                "english_title": normalize_text(program["english_title"]) or None,
                "program_url": normalize_text(program["program_url"]),
                "site_id": site_id,
                "corner_id": corner_id,
                "episode_id": episode_id,
                "series_title": series_title,
                "episode_title": episode_title,
                "episode_title_sub": title_sub,
                "episode_sub_title": normalize_text(episode.get("program_sub_title")) or None,
                "onair_at": start_at.astimezone(JST).isoformat(timespec="seconds")
                if start_at is not None
                else None,
                "closed_at": normalize_text(episode.get("closed_at")) or None,
                "stream_url": stream_url,
                "player_url": (
                    "https://www.nhk.or.jp/radio/player/ondemand.html?p="
                    f"{site_id}_{corner_id}_{episode_id}"
                ),
                "file_name": file_name,
                "relative_path": make_relative_path(final_path),
                "expected_duration_seconds": expected,
                "description": normalize_text(data.get("series_description")) or None,
            }
        )
    return rows


def upsert_recording(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO recording (
            recording_id, program_id, program_name, program_abbreviation,
            english_title, program_url, site_id, corner_id, episode_id,
            series_title, episode_title, episode_title_sub, episode_sub_title,
            onair_at, closed_at, stream_url, player_url, file_name,
            relative_path, status, description, expected_duration_seconds,
            created_at, updated_at
        ) VALUES (
            :recording_id, :program_id, :program_name, :program_abbreviation,
            :english_title, :program_url, :site_id, :corner_id, :episode_id,
            :series_title, :episode_title, :episode_title_sub, :episode_sub_title,
            :onair_at, :closed_at, :stream_url, :player_url, :file_name,
            :relative_path, 'pending', :description, :expected_duration_seconds,
            :created_at, :updated_at
        )
        ON CONFLICT(recording_id) DO UPDATE SET
            program_id = excluded.program_id,
            program_name = excluded.program_name,
            program_abbreviation = excluded.program_abbreviation,
            english_title = excluded.english_title,
            program_url = excluded.program_url,
            site_id = excluded.site_id,
            corner_id = excluded.corner_id,
            episode_id = excluded.episode_id,
            series_title = excluded.series_title,
            episode_title = excluded.episode_title,
            episode_title_sub = excluded.episode_title_sub,
            episode_sub_title = excluded.episode_sub_title,
            onair_at = excluded.onair_at,
            closed_at = excluded.closed_at,
            stream_url = excluded.stream_url,
            player_url = excluded.player_url,
            file_name = CASE
                WHEN recording.status = 'downloaded' THEN recording.file_name
                ELSE excluded.file_name
            END,
            relative_path = CASE
                WHEN recording.status = 'downloaded' THEN recording.relative_path
                ELSE excluded.relative_path
            END,
            description = excluded.description,
            expected_duration_seconds = COALESCE(
                excluded.expected_duration_seconds,
                recording.expected_duration_seconds
            ),
            updated_at = excluded.updated_at
        """,
        {**row, "created_at": timestamp, "updated_at": timestamp},
    )


def load_metadata(
    conn: sqlite3.Connection,
    ctx: RunContext,
    program_selector: str | None,
) -> tuple[int, int]:
    programs = selected_programs(conn, program_selector)
    inserted_or_updated = 0
    for program in programs:
        site_id = normalize_text(program["site_id"])
        corner_id = normalize_text(program["corner_id"]) or "01"
        if not site_id:
            site_id, corner_id = extract_site_corner(normalize_text(program["program_url"]))
        params = urllib.parse.urlencode(
            {"site_id": site_id, "corner_site_id": corner_id}
        )
        url = NHK_API + "?" + params
        ctx.logger.info(
            "LOAD PROGRAM program_id=%s name=%s site_id=%s corner_id=%s",
            program["program_id"],
            program["program_name"],
            site_id,
            corner_id,
        )
        data = http_json(url, normalize_text(program["program_url"]), ctx.logger)
        rows = metadata_rows_for_program(program, data)
        ctx.logger.info(
            "NHK metadata fetched: title=%s episodes=%s",
            normalize_text(data.get("title")),
            len(rows),
        )
        with conn:
            for row in rows:
                upsert_recording(conn, row)
                inserted_or_updated += 1
                ctx.logger.debug(
                    "RECORDING UPSERT recording_id=%s onair_at=%s title=%s",
                    row["recording_id"],
                    row["onair_at"],
                    row["episode_title"],
                )
    return len(programs), inserted_or_updated


def find_executable(name: str, preferred: Sequence[Path]) -> Path | None:
    for path in preferred:
        if path and path.exists() and path.is_file():
            return path.resolve()
    located = shutil.which(name)
    return Path(located).resolve() if located else None


def resolve_ffmpeg() -> Path:
    env = os.environ.get("RADIO_FFMPEG")
    preferred = [
        Path(env) if env else Path("__missing__"),
        ROOT / "bin" / "ffmpeg.exe",
        DEFAULT_FFMPEG,
    ]
    path = find_executable("ffmpeg", preferred)
    if path is None:
        raise RadioError(
            "ffmpeg was not found. Set RADIO_FFMPEG or place ffmpeg.exe at "
            f"{DEFAULT_FFMPEG}"
        )
    return path


def resolve_ffprobe(ffmpeg: Path) -> Path | None:
    env = os.environ.get("RADIO_FFPROBE")
    preferred = [
        Path(env) if env else Path("__missing__"),
        ffmpeg.with_name("ffprobe.exe"),
        ffmpeg.with_name("ffprobe"),
        ROOT / "bin" / "ffprobe.exe",
    ]
    return find_executable("ffprobe", preferred)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_duration(ffprobe: Path | None, path: Path, logger: logging.Logger) -> float | None:
    if ffprobe is None:
        logger.warning("ffprobe was not found; duration validation is limited")
        return None
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        logger.warning("ffprobe failed: %s", completed.stderr.strip())
        return None
    with contextlib.suppress(ValueError):
        return float(completed.stdout.strip())
    return None


def fetch_playlist_text(url: str, referer: str) -> tuple[str, str] | None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": referer},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8-sig", errors="replace"), response.geturl()
    except Exception:
        return None


def count_hls_segments(url: str, referer: str) -> int | None:
    current = fetch_playlist_text(url, referer)
    if current is None:
        return None
    text, resolved_url = current
    media_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    segment_lines = [line for line in media_lines if not line.lower().endswith(".m3u8")]
    if segment_lines:
        return len(segment_lines)
    variants = [line for line in media_lines if line.lower().endswith(".m3u8")]
    if variants:
        child_url = urllib.parse.urljoin(resolved_url, variants[0])
        child = fetch_playlist_text(child_url, referer)
        if child is not None:
            child_text, _ = child
            return sum(
                1
                for line in child_text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
    return None


def final_path_for_recording(row: Mapping[str, Any]) -> Path:
    relative = Path(normalize_text(row["relative_path"]))
    return relative if relative.is_absolute() else ROOT / relative


def mark_existing_file_downloaded(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    path: Path,
    ffprobe: Path | None,
    logger: logging.Logger,
) -> None:
    size = path.stat().st_size
    duration = probe_duration(ffprobe, path, logger)
    digest = sha256_file(path)
    conn.execute(
        """
        UPDATE recording
           SET status = 'downloaded', downloaded_at = COALESCE(downloaded_at, ?),
               file_size = ?, actual_duration_seconds = COALESCE(?, actual_duration_seconds),
               sha256 = ?, last_error = NULL, updated_at = ?
         WHERE recording_id = ?
        """,
        (now_iso(), size, duration, digest, now_iso(), row["recording_id"]),
    )
    conn.commit()


def execute_ffmpeg(
    ffmpeg: Path,
    stream_url: str,
    staging_path: Path,
    logger: logging.Logger,
) -> tuple[list[str], subprocess.CompletedProcess[str]]:
    commands = [
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            stream_url,
            "-vn",
            "-c:a",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(staging_path),
        ],
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            stream_url,
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(staging_path),
        ],
    ]
    last: subprocess.CompletedProcess[str] | None = None
    last_command: list[str] = commands[-1]
    for index, command in enumerate(commands, start=1):
        with contextlib.suppress(OSError):
            staging_path.unlink()
        logger.info("FFMPEG START mode=%s output=%s", index, staging_path)
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60 * 60,
            check=False,
        )
        last = completed
        last_command = command
        if completed.returncode == 0 and staging_path.exists() and staging_path.stat().st_size > 0:
            return command, completed
        logger.warning(
            "FFMPEG mode=%s failed exit_code=%s stderr=%s",
            index,
            completed.returncode,
            completed.stderr[-4000:],
        )
    assert last is not None
    return last_command, last


def next_attempt_no(conn: sqlite3.Connection, recording_id: str) -> int:
    value = conn.execute(
        "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM download_attempt WHERE recording_id = ?",
        (recording_id,),
    ).fetchone()[0]
    return int(value)


def download_pending(
    conn: sqlite3.Connection,
    ctx: RunContext,
    program_selector: str | None,
    limit: int | None,
    retry_errors: bool,
) -> tuple[int, int, int]:
    ffmpeg = resolve_ffmpeg()
    ffprobe = resolve_ffprobe(ffmpeg)
    statuses = ["pending"] + (["error"] if retry_errors else [])
    placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = list(statuses)
    sql = (
        "SELECT r.*, p.output_directory, p.directory_name "
        "FROM recording r JOIN program p ON p.program_id = r.program_id "
        f"WHERE r.status IN ({placeholders}) AND p.is_active = 1"
    )
    if program_selector:
        sql += (
            " AND (CAST(p.program_id AS TEXT) = ? OR upper(p.program_abbreviation) = upper(?) "
            "OR p.program_name = ?)"
        )
        params.extend([program_selector, program_selector, program_selector])
    sql += " ORDER BY COALESCE(r.onair_at, ''), r.recording_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = list(conn.execute(sql, params))
    success = 0
    skipped = 0
    errors = 0
    ctx.logger.info("DOWNLOAD PLAN count=%s ffmpeg=%s ffprobe=%s", len(rows), ffmpeg, ffprobe)
    run_work = WORK_DIR / ctx.run_id
    run_work.mkdir(parents=True, exist_ok=True)
    for row in rows:
        recording_id = normalize_text(row["recording_id"])
        final_path = final_path_for_recording(row)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists() and final_path.stat().st_size > 0:
            ctx.logger.info("SKIP EXISTING recording_id=%s file=%s", recording_id, final_path)
            mark_existing_file_downloaded(conn, row, final_path, ffprobe, ctx.logger)
            skipped += 1
            continue
        staging_dir = run_work / safe_filename(recording_id, 160)
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_path = staging_dir / (safe_filename(recording_id, 160) + ".m4a")
        attempt_no = next_attempt_no(conn, recording_id)
        started_at = now_iso()
        conn.execute(
            "UPDATE recording SET status='downloading', last_error=NULL, updated_at=? WHERE recording_id=?",
            (started_at, recording_id),
        )
        conn.execute(
            """
            INSERT INTO download_attempt(
                run_id, recording_id, attempt_no, started_at, status,
                staging_path, final_path
            ) VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                ctx.run_id,
                recording_id,
                attempt_no,
                started_at,
                str(staging_path),
                str(final_path),
            ),
        )
        conn.commit()
        command: list[str] = []
        try:
            command, completed = execute_ffmpeg(
                ffmpeg,
                normalize_text(row["stream_url"]),
                staging_path,
                ctx.logger,
            )
            if completed.returncode != 0 or not staging_path.exists():
                raise RadioError(
                    f"ffmpeg failed exit_code={completed.returncode}: {completed.stderr[-2000:]}"
                )
            size = staging_path.stat().st_size
            if size <= 0:
                raise RadioError("Downloaded file is empty")
            actual_duration = probe_duration(ffprobe, staging_path, ctx.logger)
            expected_duration = row["expected_duration_seconds"]
            if actual_duration is not None and actual_duration <= 0:
                raise RadioError(f"Invalid duration: {actual_duration}")
            if actual_duration is not None and expected_duration:
                tolerance = max(15.0, float(expected_duration) * 0.20)
                if abs(actual_duration - float(expected_duration)) > tolerance:
                    raise RadioError(
                        "Duration differs from the NHK metadata: "
                        f"expected={expected_duration} actual={actual_duration} tolerance={tolerance}"
                    )
            digest = sha256_file(staging_path)
            segment_count = count_hls_segments(
                normalize_text(row["stream_url"]),
                normalize_text(row["program_url"]),
            )
            os.replace(staging_path, final_path)
            completed_at = now_iso()
            conn.execute(
                """
                UPDATE recording
                   SET status='downloaded', downloaded_at=?, file_size=?,
                       actual_duration_seconds=?, segment_count=?, sha256=?,
                       last_error=NULL, updated_at=?
                 WHERE recording_id=?
                """,
                (
                    completed_at,
                    size,
                    actual_duration,
                    segment_count,
                    digest,
                    completed_at,
                    recording_id,
                ),
            )
            conn.execute(
                """
                UPDATE download_attempt
                   SET ended_at=?, status='success', ffmpeg_command=?, exit_code=0,
                       file_size=?, actual_duration_seconds=?, sha256=?
                 WHERE run_id=? AND recording_id=? AND attempt_no=?
                """,
                (
                    completed_at,
                    subprocess.list2cmdline(command),
                    size,
                    actual_duration,
                    digest,
                    ctx.run_id,
                    recording_id,
                    attempt_no,
                ),
            )
            conn.commit()
            success += 1
            ctx.logger.info(
                "DOWNLOAD SUCCESS recording_id=%s bytes=%s duration=%s file=%s",
                recording_id,
                size,
                actual_duration,
                final_path,
            )
        except Exception as exc:
            errors += 1
            error_text = f"{type(exc).__name__}: {exc}"
            ctx.logger.error("DOWNLOAD ERROR recording_id=%s %s", recording_id, error_text)
            completed_at = now_iso()
            conn.execute(
                "UPDATE recording SET status='error', last_error=?, updated_at=? WHERE recording_id=?",
                (error_text[:4000], completed_at, recording_id),
            )
            conn.execute(
                """
                UPDATE download_attempt
                   SET ended_at=?, status='error', ffmpeg_command=?,
                       exit_code=?, error_message=?
                 WHERE run_id=? AND recording_id=? AND attempt_no=?
                """,
                (
                    completed_at,
                    subprocess.list2cmdline(command) if command else None,
                    getattr(locals().get("completed", None), "returncode", None),
                    error_text[:4000],
                    ctx.run_id,
                    recording_id,
                    attempt_no,
                ),
            )
            conn.commit()
        finally:
            with contextlib.suppress(OSError):
                if staging_path.exists():
                    staging_path.unlink()
            with contextlib.suppress(OSError):
                staging_dir.rmdir()
    with contextlib.suppress(OSError):
        run_work.rmdir()
    return success, skipped, errors


def verify_database(conn: sqlite3.Connection, logger: logging.Logger) -> tuple[bool, str]:
    lines: list[str] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    lines.append(f"integrity_check={integrity}")
    foreign_rows = list(conn.execute("PRAGMA foreign_key_check"))
    lines.append(f"foreign_key_errors={len(foreign_rows)}")
    for table in ("program", "recording", "run", "download_attempt"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        lines.append(f"{table}_count={count}")
    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM recording GROUP BY status ORDER BY status"
    ).fetchall()
    lines.append(
        "recording_status_counts="
        + json.dumps({row["status"]: row["count"] for row in status_rows}, ensure_ascii=False)
    )
    missing: list[str] = []
    for row in conn.execute(
        "SELECT recording_id, relative_path FROM recording WHERE status='downloaded'"
    ):
        path = final_path_for_recording(row)
        if not path.exists() or path.stat().st_size <= 0:
            missing.append(f"{row['recording_id']}={path}")
    lines.append(f"downloaded_missing_files={len(missing)}")
    lines.extend("missing=" + item for item in missing[:100])
    ok = integrity == "ok" and not foreign_rows and not missing
    text = "\n".join(lines) + "\n"
    for line in lines:
        logger.info("VERIFY %s", line)
    return ok, text


def create_validation_zip(
    conn: sqlite3.Connection,
    ctx: RunContext,
    verification_text: str,
) -> Path:
    output = LOG_DIR / f"{ctx.run_id}_validation.zip"
    summary = {
        "version": VERSION,
        "run_id": ctx.run_id,
        "command": ctx.command_name,
        "created_at": now_iso(),
        "database": str(DATABASE_PATH),
        "log": str(ctx.log_path),
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if ctx.log_path.exists():
            archive.write(ctx.log_path, arcname=ctx.log_path.name)
        if SCHEMA_PATH.exists():
            archive.write(SCHEMA_PATH, arcname="schema.sql")
        if SEED_PATH.exists():
            archive.write(SEED_PATH, arcname="seed_programs.sql")
        archive.writestr(
            "verification.txt",
            verification_text.encode("utf-8-sig"),
        )
        archive.writestr(
            "run_summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        program_rows = [dict(row) for row in conn.execute("SELECT * FROM v_program ORDER BY program_id")]
        archive.writestr(
            "program_summary.json",
            json.dumps(program_rows, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        recent = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM v_recording ORDER BY COALESCE(onair_at, '') DESC LIMIT 500"
            )
        ]
        archive.writestr(
            "recording_recent.json",
            json.dumps(recent, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return output


def table_columns(conn: sqlite3.Connection, table_name: str) -> dict[str, str]:
    return {
        normalize_text(row["name"]).lower(): normalize_text(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})")
    }


def first_existing_table(conn: sqlite3.Connection, names: Sequence[str]) -> str | None:
    available = {
        normalize_text(row[0]).lower(): normalize_text(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for name in names:
        if name.lower() in available:
            return available[name.lower()]
    return None


def value_from(row: sqlite3.Row, columns: Mapping[str, str], *names: str) -> Any:
    for name in names:
        actual = columns.get(name.lower())
        if actual is not None:
            return row[actual]
    return None


def migrate_sqlite(
    target: sqlite3.Connection,
    source_path: Path,
    logger: logging.Logger,
) -> tuple[int, int]:
    if not source_path.exists():
        raise RadioError(f"Source SQLite file does not exist: {source_path}")
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    program_count = 0
    recording_count = 0
    try:
        program_table = first_existing_table(source, ("program", "programs"))
        recording_table = first_existing_table(source, ("recording", "recordings"))
        if program_table:
            columns = table_columns(source, program_table)
            for source_row in source.execute(f'SELECT * FROM "{program_table}"'):
                name = normalize_text(value_from(source_row, columns, "program_name", "name"))
                url = normalize_text(value_from(source_row, columns, "program_url", "url"))
                abbreviation = normalize_text(
                    value_from(source_row, columns, "program_abbreviation", "abbr", "abbreviation")
                )
                if not name or not url or not abbreviation:
                    continue
                site_id = normalize_text(value_from(source_row, columns, "site_id"))
                corner_id = normalize_text(value_from(source_row, columns, "corner_id")) or "01"
                if not site_id:
                    site_id, corner_id = extract_site_corner(url)
                source_id = value_from(source_row, columns, "program_id", "id")
                existing = target.execute(
                    "SELECT program_id FROM program WHERE program_name=? OR program_url=?",
                    (name, url),
                ).fetchone()
                program_id = int(existing[0]) if existing else (int(source_id) if source_id else None)
                timestamp = now_iso()
                if program_id is None:
                    target.execute(
                        """
                        INSERT INTO program(
                            program_name, program_url, is_active, output_directory,
                            directory_name, program_abbreviation, english_title,
                            site_id, corner_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            name,
                            url,
                            1 if normalize_text(value_from(source_row, columns, "is_active")) not in ("0", "0.0") else 0,
                            normalize_text(value_from(source_row, columns, "output_directory")) or "audio",
                            normalize_text(value_from(source_row, columns, "directory_name", "dir_name")) or name,
                            abbreviation,
                            normalize_text(value_from(source_row, columns, "english_title")) or None,
                            site_id,
                            corner_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    target.execute(
                        """
                        INSERT INTO program(
                            program_id, program_name, program_url, is_active,
                            output_directory, directory_name, program_abbreviation,
                            english_title, site_id, corner_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(program_id) DO UPDATE SET
                            program_name=excluded.program_name,
                            program_url=excluded.program_url,
                            is_active=excluded.is_active,
                            output_directory=excluded.output_directory,
                            directory_name=excluded.directory_name,
                            program_abbreviation=excluded.program_abbreviation,
                            english_title=excluded.english_title,
                            site_id=excluded.site_id,
                            corner_id=excluded.corner_id,
                            updated_at=excluded.updated_at
                        """,
                        (
                            program_id,
                            name,
                            url,
                            1 if normalize_text(value_from(source_row, columns, "is_active")) not in ("0", "0.0") else 0,
                            normalize_text(value_from(source_row, columns, "output_directory")) or "audio",
                            normalize_text(value_from(source_row, columns, "directory_name", "dir_name")) or name,
                            abbreviation,
                            normalize_text(value_from(source_row, columns, "english_title")) or None,
                            site_id,
                            corner_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                program_count += 1
        if recording_table:
            columns = table_columns(source, recording_table)
            for source_row in source.execute(f'SELECT * FROM "{recording_table}"'):
                recording_id = normalize_text(value_from(source_row, columns, "recording_id"))
                site_id = normalize_text(value_from(source_row, columns, "site_id"))
                corner_id = normalize_text(value_from(source_row, columns, "corner_id")) or "01"
                episode_id = normalize_text(value_from(source_row, columns, "episode_id"))
                if not recording_id and site_id and episode_id:
                    recording_id = f"{site_id}_{corner_id}_{episode_id}"
                if not recording_id:
                    continue
                program_name = normalize_text(value_from(source_row, columns, "program_name"))
                abbreviation = normalize_text(
                    value_from(source_row, columns, "program_abbreviation", "abbr")
                )
                program = target.execute(
                    """
                    SELECT * FROM program
                     WHERE program_name=? OR program_abbreviation=?
                     ORDER BY CASE WHEN program_name=? THEN 0 ELSE 1 END
                     LIMIT 1
                    """,
                    (program_name, abbreviation, program_name),
                ).fetchone()
                if program is None:
                    logger.warning("MIGRATE SKIP recording=%s program=%s", recording_id, program_name)
                    continue
                final_name = normalize_text(value_from(source_row, columns, "file_name")) or f"{recording_id}.m4a"
                relative_path = normalize_text(value_from(source_row, columns, "relative_path"))
                if not relative_path:
                    relative_path = str(Path("audio") / normalize_text(program["directory_name"]) / final_name)
                timestamp = now_iso()
                target.execute(
                    """
                    INSERT INTO recording(
                        recording_id, program_id, program_name, program_abbreviation,
                        english_title, program_url, site_id, corner_id, episode_id,
                        series_title, episode_title, episode_title_sub,
                        episode_sub_title, onair_at, closed_at, stream_url,
                        player_url, file_name, relative_path, downloaded_at, status,
                        description, file_size, expected_duration_seconds,
                        actual_duration_seconds, segment_count, sha256, last_error,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    ON CONFLICT(recording_id) DO UPDATE SET
                        status=excluded.status,
                        downloaded_at=COALESCE(excluded.downloaded_at, recording.downloaded_at),
                        file_size=COALESCE(excluded.file_size, recording.file_size),
                        actual_duration_seconds=COALESCE(
                            excluded.actual_duration_seconds,
                            recording.actual_duration_seconds
                        ),
                        segment_count=COALESCE(excluded.segment_count, recording.segment_count),
                        sha256=COALESCE(excluded.sha256, recording.sha256),
                        last_error=excluded.last_error,
                        updated_at=excluded.updated_at
                    """,
                    (
                        recording_id,
                        program["program_id"],
                        program["program_name"],
                        program["program_abbreviation"],
                        normalize_text(value_from(source_row, columns, "english_title")) or program["english_title"],
                        normalize_text(value_from(source_row, columns, "program_url")) or program["program_url"],
                        site_id or program["site_id"],
                        corner_id,
                        episode_id or recording_id.rsplit("_", 1)[-1],
                        normalize_text(value_from(source_row, columns, "series_title")) or program["program_name"],
                        normalize_text(value_from(source_row, columns, "episode_title")) or recording_id,
                        normalize_text(value_from(source_row, columns, "episode_title_sub")) or None,
                        normalize_text(value_from(source_row, columns, "episode_sub_title")) or None,
                        normalize_text(value_from(source_row, columns, "onair_at", "onair_date")) or None,
                        normalize_text(value_from(source_row, columns, "closed_at")) or None,
                        normalize_text(value_from(source_row, columns, "stream_url")) or "",
                        normalize_text(value_from(source_row, columns, "player_url")) or None,
                        final_name,
                        relative_path,
                        normalize_text(value_from(source_row, columns, "downloaded_at")) or None,
                        normalize_status(value_from(source_row, columns, "status")),
                        normalize_text(value_from(source_row, columns, "description")) or None,
                        value_from(source_row, columns, "file_size"),
                        value_from(source_row, columns, "expected_duration_seconds"),
                        value_from(source_row, columns, "actual_duration_seconds"),
                        value_from(source_row, columns, "segment_count"),
                        normalize_text(value_from(source_row, columns, "sha256")) or None,
                        normalize_text(value_from(source_row, columns, "last_error")) or None,
                        timestamp,
                        timestamp,
                    ),
                )
                recording_count += 1
        target.commit()
    finally:
        source.close()
    logger.info(
        "SQLITE MIGRATION COMPLETE source=%s programs=%s recordings=%s",
        source_path,
        program_count,
        recording_count,
    )
    return program_count, recording_count


def run_self_test(logger: logging.Logger) -> str:
    ensure_directories()
    fd, temp_name = tempfile.mkstemp(prefix="radio_self_test_", suffix=".db", dir=DATABASE_DIR)
    os.close(fd)
    path = Path(temp_name)
    with contextlib.suppress(OSError):
        path.unlink()
    try:
        initialize_database(path, seed=False)
        with connect_db(path) as conn:
            timestamp = now_iso()
            conn.execute(
                """
                INSERT INTO program(
                    program_id, program_name, program_url, is_active,
                    output_directory, directory_name, program_abbreviation,
                    english_title, site_id, corner_id, created_at, updated_at
                ) VALUES (1, 'TEST', 'https://example.invalid/p/rs/TEST000001/list/',
                          1, 'audio', 'test', 'TST', 'Test', 'TEST000001', '01', ?, ?)
                """,
                (timestamp, timestamp),
            )
            fixture = {
                "recording_id": "TEST000001_01_1",
                "program_id": 1,
                "program_name": "TEST",
                "program_abbreviation": "TST",
                "english_title": "Test",
                "program_url": "https://example.invalid/p/rs/TEST000001/list/",
                "site_id": "TEST000001",
                "corner_id": "01",
                "episode_id": "1",
                "series_title": "TEST",
                "episode_title": "TEST Episode 1",
                "episode_title_sub": "Episode 1",
                "episode_sub_title": None,
                "onair_at": "2026-07-26T12:00:00+09:00",
                "closed_at": None,
                "stream_url": "https://example.invalid/test.m3u8",
                "player_url": "https://example.invalid/player",
                "file_name": "260726_Episode 1.m4a",
                "relative_path": "audio/test/260726_Episode 1.m4a",
                "expected_duration_seconds": 300.0,
                "description": "self-test",
            }
            upsert_recording(conn, fixture)
            conn.commit()
            count = conn.execute("SELECT COUNT(*) FROM recording").fetchone()[0]
            row = conn.execute("SELECT * FROM recording").fetchone()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign = list(conn.execute("PRAGMA foreign_key_check"))
            if count != 1 or row["status"] != "pending" or integrity != "ok" or foreign:
                raise RadioError(
                    f"Self-test failed: count={count} status={row['status']} "
                    f"integrity={integrity} foreign={foreign}"
                )
            text = (
                "SELF_TEST=PASS\n"
                f"recording_count={count}\n"
                f"recording_id={row['recording_id']}\n"
                f"status={row['status']}\n"
                f"integrity_check={integrity}\n"
            )
            logger.info(text.replace("\n", " | ").strip(" |"))
            return text
    finally:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(str(path) + suffix).unlink()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless NHK Radio batch using one local SQLite database."
    )
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create radio.db and seed the program table.")

    load = subparsers.add_parser("load", help="Load NHK metadata into recording.")
    load.add_argument("--program", help="program_id, abbreviation, or exact program name")

    download = subparsers.add_parser("download", help="Download pending recordings.")
    download.add_argument("--program", help="program_id, abbreviation, or exact program name")
    download.add_argument("--limit", type=int)
    download.add_argument("--retry-errors", action="store_true")

    run = subparsers.add_parser("run", help="Load metadata and download pending rows.")
    run.add_argument("--program", help="program_id, abbreviation, or exact program name")
    run.add_argument("--limit", type=int)
    run.add_argument("--retry-errors", action="store_true")

    subparsers.add_parser("verify", help="Run SQLite and file integrity checks.")

    migrate = subparsers.add_parser(
        "migrate-sqlite",
        help="Import program/recording rows from an earlier SQLite database.",
    )
    migrate.add_argument("--source", type=Path, required=True)

    subparsers.add_parser(
        "self-test",
        help="Create a temporary database and prove that a recording row can be inserted.",
    )
    subparsers.add_parser("force-unlock", help="Remove a stale process lock.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_directories()
    if args.command == "force-unlock":
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
            print(f"Removed: {LOCK_PATH}")
        else:
            print(f"Lock does not exist: {LOCK_PATH}")
        return 0

    run_id = new_run_id()
    logger, log_path = create_logger(run_id)
    ctx = RunContext(run_id, args.command, now_iso(), log_path, logger)
    logger.info("================ RUN START ================")
    logger.info("VERSION=%s", VERSION)
    logger.info("SCRIPT=%s", Path(__file__).resolve())
    logger.info("ROOT=%s", ROOT)
    logger.info("DATABASE=%s", args.database)
    logger.info("COMMAND=%s", args.command)

    if args.command == "self-test":
        try:
            with ProcessLock(LOCK_PATH, run_id):
                run_self_test(logger)
            logger.info("================ RUN END status=success ================")
            return 0
        except Exception as exc:
            logger.critical(
                "SELF TEST FAILED type=%s message=%s\n%s",
                type(exc).__name__,
                exc,
                traceback.format_exc(),
            )
            return 1

    db_path: Path = args.database.resolve()
    original_global_db = globals()["DATABASE_PATH"]
    globals()["DATABASE_PATH"] = db_path
    conn: sqlite3.Connection | None = None
    program_count = 0
    planned_count = 0
    success_count = 0
    skip_count = 0
    error_count = 0
    exit_code = 1
    status = "error"
    description: str | None = None
    validation_zip: Path | None = None
    try:
        with ProcessLock(LOCK_PATH, run_id):
            initialize_database(db_path, seed=True)
            conn = connect_db(db_path)
            start_run(conn, ctx)
            if args.command == "init":
                program_count = conn.execute("SELECT COUNT(*) FROM program").fetchone()[0]
                status = "success"
                exit_code = 0
            elif args.command == "load":
                program_count, planned_count = load_metadata(conn, ctx, args.program)
                success_count = planned_count
                status = "success"
                exit_code = 0
            elif args.command == "download":
                program_count = len(selected_programs(conn, args.program))
                success_count, skip_count, error_count = download_pending(
                    conn,
                    ctx,
                    args.program,
                    args.limit,
                    args.retry_errors,
                )
                planned_count = success_count + skip_count + error_count
                status = "success" if error_count == 0 else ("partial" if success_count or skip_count else "error")
                exit_code = 0 if error_count == 0 else 2
            elif args.command == "run":
                program_count, loaded_count = load_metadata(conn, ctx, args.program)
                success_count, skip_count, error_count = download_pending(
                    conn,
                    ctx,
                    args.program,
                    args.limit,
                    args.retry_errors,
                )
                planned_count = success_count + skip_count + error_count
                description = f"metadata_rows={loaded_count}"
                status = "success" if error_count == 0 else ("partial" if success_count or skip_count else "error")
                exit_code = 0 if error_count == 0 else 2
            elif args.command == "verify":
                ok, verification = verify_database(conn, logger)
                status = "success" if ok else "error"
                exit_code = 0 if ok else 3
                description = verification
            elif args.command == "migrate-sqlite":
                program_count, success_count = migrate_sqlite(conn, args.source.resolve(), logger)
                planned_count = success_count
                status = "success"
                exit_code = 0
            else:
                raise RadioError(f"Unsupported command: {args.command}")

            ok, verification_text = verify_database(conn, logger)
            if not ok and exit_code == 0:
                status = "error"
                exit_code = 3
            finish_run(
                conn,
                ctx,
                status=status,
                exit_code=exit_code,
                program_count=program_count,
                planned_count=planned_count,
                success_count=success_count,
                skip_count=skip_count,
                error_count=error_count,
                description=description,
            )
            validation_zip = create_validation_zip(conn, ctx, verification_text)
            finish_run(
                conn,
                ctx,
                status=status,
                exit_code=exit_code,
                program_count=program_count,
                planned_count=planned_count,
                success_count=success_count,
                skip_count=skip_count,
                error_count=error_count,
                description=description,
                validation_zip_path=validation_zip,
            )
    except Exception as exc:
        description = f"{type(exc).__name__}: {exc}"
        logger.critical(
            "UNHANDLED_EXCEPTION type=%s message=%s\n%s",
            type(exc).__name__,
            exc,
            traceback.format_exc(),
        )
        if conn is not None:
            with contextlib.suppress(Exception):
                finish_run(
                    conn,
                    ctx,
                    status="error",
                    exit_code=1,
                    program_count=program_count,
                    planned_count=planned_count,
                    success_count=success_count,
                    skip_count=skip_count,
                    error_count=max(1, error_count),
                    description=description,
                )
        exit_code = 1
        status = "error"
    finally:
        if conn is not None:
            conn.close()
        globals()["DATABASE_PATH"] = original_global_db
        logger.info(
            "RUN RESULT status=%s exit_code=%s programs=%s planned=%s success=%s skip=%s error=%s",
            status,
            exit_code,
            program_count,
            planned_count,
            success_count,
            skip_count,
            error_count,
        )
        if validation_zip:
            logger.info("VALIDATION PACKAGE=%s", validation_zip)
        logger.info("================ RUN END ================")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
