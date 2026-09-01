# English-only source file. Runtime data from NHK and Excel may contain Unicode.
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen

PROGRAM_VERSION = "V34_COMPLETE_REWRITE_EXPLICIT_SAVE_NO_AUTOSAVE_GATE"

SCRIPT_PATH = Path(__file__).resolve()
BASE_DIRECTORY = Path.cwd()
WORKBOOK_NAME = "radio_recording.xlsx"
WORKBOOK_PATH = BASE_DIRECTORY / WORKBOOK_NAME
PROGRAM_SHEET_NAME = "program"
RECORDING_SHEET_NAME = "recording"
HEADER_ROW = 2
DATA_START_ROW = 3
XL_UP = -4162
XL_TO_LEFT = -4159

AUDIO_ROOT_DIRECTORY = BASE_DIRECTORY / "audio"
LOG_DIRECTORY = BASE_DIRECTORY / "log"
RUN_LOG_DIRECTORY = LOG_DIRECTORY / "runs"
WORK_ROOT_DIRECTORY = BASE_DIRECTORY / ".nhk_download_work"
LOCK_PATH = BASE_DIRECTORY / ".nhk_download.lock"

MAX_CONCURRENT_DOWNLOADS = 15
MAX_DOWNLOAD_ATTEMPTS = 3
MAX_EXCEL_WRITE_ATTEMPTS = 3
EXCEL_WRITE_RETRY_DELAYS_SECONDS = (1.0, 3.0, 5.0)
FFMPEG_TIMEOUT_SECONDS = 1200
FFPROBE_TIMEOUT_SECONDS = 90
DURATION_TOLERANCE_RATIO = 0.005
DURATION_TOLERANCE_MIN_SECONDS = 2.0

JST = timezone(timedelta(hours=9), name="JST")
RUN_STARTED_AT_JST = datetime.now(JST)
RUN_ID = f"{RUN_STARTED_AT_JST:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"

LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
RUN_LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
WORK_ROOT_DIRECTORY.mkdir(parents=True, exist_ok=True)

LOG_PATH = LOG_DIRECTORY / f"{RUN_STARTED_AT_JST:%Y%m%d}.log"
RUN_LOG_PATH = RUN_LOG_DIRECTORY / f"{RUN_ID}.log"
RUN_SUMMARY_PATH = RUN_LOG_DIRECTORY / f"{RUN_ID}.summary.json"
VALIDATION_ZIP_PATH = RUN_LOG_DIRECTORY / f"{RUN_ID}_validation.zip"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ja,en;q=0.9",
}

PROGRAM_REQUIRED_HEADERS = [
    "program_name",
    "program_url",
    "is_active",
    "output_directory",
    "directory_name",
    "program_abbreviation",
]

RECORDING_REQUIRED_HEADERS = [
    "program_name",
    "onair_at",
    "status",
    "episode_title",
    "actual_duration_seconds",
    "downloaded_at",
    "file_name",
    "description",
    "file_size",
    "saved_directory",
    "recording_id",
    "player_url",
]


@dataclass(slots=True)
class ExistingRecording:
    row_number: int
    status: int | None


@dataclass(slots=True)
class DownloadJob:
    sequence: int
    target_row: int | None
    program_name: str
    program_url: str
    directory_name: str
    output_root: Path
    site_id: str
    corner_id: str
    recording_id: str
    episode_id: str
    episode_title: str
    onair_at: datetime | None
    stream_url: str
    player_url: str
    output_path: Path


@dataclass(slots=True)
class DownloadResult:
    job: DownloadJob
    success: bool
    file_size_bytes: int | None
    expected_duration_seconds: float | None
    actual_duration_seconds: float | None
    sha256: str
    completed_at: datetime
    error_message: str
    attempts: int


@dataclass(slots=True)
class HlsManifestInfo:
    media_playlist_url: str
    expected_duration_seconds: float
    segment_count: int
    has_endlist: bool


class JstLogFormatter(logging.Formatter):
    def formatTime(
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        value = datetime.fromtimestamp(
            record.created,
            tz=timezone.utc,
        ).astimezone(JST)
        return value.strftime(datefmt) if datefmt else value.isoformat(timespec="seconds")


def create_logger() -> logging.Logger:
    logger = logging.getLogger("nhk_download")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    formatter = JstLogFormatter(
        f"%(asctime)s JST [%(levelname)s] [run={RUN_ID}] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    daily = logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")
    daily.setLevel(logging.DEBUG)
    daily.setFormatter(formatter)

    run = logging.FileHandler(RUN_LOG_PATH, mode="w", encoding="utf-8")
    run.setLevel(logging.DEBUG)
    run.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(daily)
    logger.addHandler(run)
    return logger


LOGGER = create_logger()

ACTIVE_CHILD_PROCESSES: dict[int, subprocess.Popen[str]] = {}
ACTIVE_CHILD_LOCK = threading.Lock()
CANCEL_EVENT = threading.Event()
LOCK_FILE_DESCRIPTOR: int | None = None


def text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_header(value: Any) -> str:
    return text(value).casefold()


def parse_optional_int(value: Any) -> int | None:
    if value is None or text(value) == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def jst_now_naive() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


def excel_serial(value: datetime | None) -> float | None:
    if value is None:
        return None
    wall_clock = value.astimezone(JST).replace(tzinfo=None) if value.tzinfo else value
    return (wall_clock - datetime(1899, 12, 30)).total_seconds() / 86400.0


def bytes_to_mib(value: int | None) -> float | None:
    if value is None:
        return None
    return value / float(1024 * 1024)


def flush_logs() -> None:
    for handler in LOGGER.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def register_child_process(process: subprocess.Popen[str]) -> None:
    with ACTIVE_CHILD_LOCK:
        ACTIVE_CHILD_PROCESSES[process.pid] = process


def unregister_child_process(process: subprocess.Popen[str]) -> None:
    with ACTIVE_CHILD_LOCK:
        ACTIVE_CHILD_PROCESSES.pop(process.pid, None)


def terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def terminate_all_child_processes() -> None:
    with ACTIVE_CHILD_LOCK:
        processes = list(ACTIVE_CHILD_PROCESSES.values())
    for process in processes:
        terminate_process_tree(process)


def handle_interrupt(signum: int, frame: Any) -> None:
    del signum, frame
    CANCEL_EVENT.set()
    terminate_all_child_processes()
    raise KeyboardInterrupt


def install_interrupt_handlers() -> None:
    signal.signal(signal.SIGINT, handle_interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_interrupt)


def run_child_process(
    command: list[str],
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    if CANCEL_EVENT.is_set():
        raise RuntimeError("Operation cancelled before child process start.")

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    register_child_process(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process)
        raise
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    finally:
        unregister_child_process(process)

    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout,
        stderr,
    )


def process_is_running(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
        return True
    except OSError:
        return False


def acquire_single_instance_lock() -> None:
    global LOCK_FILE_DESCRIPTOR
    for _ in range(2):
        try:
            descriptor = os.open(
                str(LOCK_PATH),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.write(
                descriptor,
                json.dumps({"pid": os.getpid(), "run_id": RUN_ID}).encode("utf-8"),
            )
            LOCK_FILE_DESCRIPTOR = descriptor
            LOGGER.info("[PROCESS LOCK ACQUIRED] file=%s pid=%s", LOCK_PATH, os.getpid())
            return
        except FileExistsError:
            try:
                payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
                old_pid = int(payload.get("pid", 0))
            except Exception:
                old_pid = 0
            if process_is_running(old_pid):
                raise RuntimeError(
                    f"Another nhk_download.py process is already running: pid={old_pid}"
                )
            LOCK_PATH.unlink(missing_ok=True)
            LOGGER.warning("[STALE PROCESS LOCK REMOVED] old_pid=%s", old_pid)
    raise RuntimeError(f"Could not acquire process lock: {LOCK_PATH}")


def release_single_instance_lock() -> None:
    global LOCK_FILE_DESCRIPTOR
    if LOCK_FILE_DESCRIPTOR is not None:
        try:
            os.close(LOCK_FILE_DESCRIPTOR)
        except OSError:
            pass
        LOCK_FILE_DESCRIPTOR = None
    try:
        LOCK_PATH.unlink(missing_ok=True)
        LOGGER.info("[PROCESS LOCK RELEASED] file=%s", LOCK_PATH)
    except OSError:
        LOGGER.exception("Could not remove process lock: %s", LOCK_PATH)


def import_excel_modules() -> tuple[Any, Any, Any]:
    try:
        import pythoncom
        import pywintypes
        import win32com.client
    except Exception as error:
        raise RuntimeError(
            "Could not import pywin32. Run: py -m pip install pywin32"
        ) from error
    return pythoncom, pywintypes, win32com.client


def list_sheet_names(workbook: Any) -> list[str]:
    return [
        text(workbook.Worksheets.Item(index).Name)
        for index in range(1, workbook.Worksheets.Count + 1)
    ]


def get_sheet(workbook: Any, sheet_name: str) -> Any | None:
    for index in range(1, workbook.Worksheets.Count + 1):
        sheet = workbook.Worksheets.Item(index)
        if text(sheet.Name).casefold() == sheet_name.casefold():
            return sheet
    return None


def connect_or_open_workbook(
    pywintypes: Any,
    win32_client: Any,
    pythoncom: Any,
) -> tuple[Any, Any, bool]:
    if not WORKBOOK_PATH.is_file():
        raise RuntimeError(f"Excel workbook was not found: {WORKBOOK_PATH}")

    workbook = None
    excel = None
    opened_by_program = False

    try:
        workbook = win32_client.GetObject(str(WORKBOOK_PATH))
        excel = workbook.Application
    except Exception:
        workbook = None

    if workbook is None:
        try:
            excel = win32_client.GetActiveObject("Excel.Application")
        except (pywintypes.com_error, OSError):
            excel = win32_client.DispatchEx("Excel.Application")
        excel.Visible = True
        try:
            workbook = excel.Workbooks.Open(
                str(WORKBOOK_PATH),
                UpdateLinks=0,
                ReadOnly=False,
                IgnoreReadOnlyRecommended=True,
                AddToMru=False,
            )
            opened_by_program = True
        except Exception as error:
            raise RuntimeError(
                f"Could not open the Excel workbook: {WORKBOOK_PATH}"
            ) from error

    if workbook is None or excel is None:
        raise RuntimeError(f"Could not connect to the Excel workbook: {WORKBOOK_PATH}")

    excel.Visible = True
    pythoncom.PumpWaitingMessages()

    if bool(workbook.ReadOnly):
        raise RuntimeError(f"The workbook is read-only: {WORKBOOK_PATH}")

    try:
        autosave_value: bool | str = bool(workbook.AutoSaveOn)
    except Exception as error:
        autosave_value = f"unavailable:{type(error).__name__}"

    LOGGER.info(
        "Excel workbook ready: expected_local_path=%s actual_fullname=%s "
        "opened_by_program=%s autosave=%s autosave_required=False explicit_save=True",
        WORKBOOK_PATH,
        text(workbook.FullName),
        opened_by_program,
        autosave_value,
    )
    LOGGER.info("Excel worksheets: %s", ", ".join(list_sheet_names(workbook)))
    return excel, workbook, opened_by_program


def read_header_map(sheet: Any) -> tuple[dict[str, int], int]:
    last_column = int(
        sheet.Cells(HEADER_ROW, sheet.Columns.Count).End(XL_TO_LEFT).Column
    )
    result: dict[str, int] = {}
    for column in range(1, max(1, last_column) + 1):
        raw = sheet.Cells(HEADER_ROW, column).Value2
        if text(raw) == "":
            continue
        normalized = normalize_header(raw)
        if normalized in result:
            raise RuntimeError(
                f"{sheet.Name} worksheet has a duplicate header: {raw}"
            )
        result[normalized] = column
    if not result:
        raise RuntimeError(f"{sheet.Name} worksheet has no headers on row {HEADER_ROW}.")
    return result, max(result.values())


def require_headers(
    sheet_name: str,
    header_map: dict[str, int],
    required_headers: list[str],
) -> None:
    missing = [
        header
        for header in required_headers
        if normalize_header(header) not in header_map
    ]
    if missing:
        raise RuntimeError(
            f"{sheet_name} worksheet is missing required headers: "
            + ", ".join(missing)
        )


def get_cell_by_header(
    sheet: Any,
    row_number: int,
    header_map: dict[str, int],
    header_name: str,
) -> Any:
    return sheet.Cells(
        row_number,
        header_map[normalize_header(header_name)],
    ).Value2


def last_nonempty_row(sheet: Any, column: int) -> int:
    return int(sheet.Cells(sheet.Rows.Count, column).End(XL_UP).Row)


def last_occupied_row(sheet: Any, header_map: dict[str, int]) -> int:
    values = [last_nonempty_row(sheet, column) for column in header_map.values()]
    return max(max(values, default=HEADER_ROW), HEADER_ROW)


def read_program_rows(program_sheet: Any) -> list[dict[str, str]]:
    header_map, _ = read_header_map(program_sheet)
    require_headers(PROGRAM_SHEET_NAME, header_map, PROGRAM_REQUIRED_HEADERS)

    last_row = max(
        last_nonempty_row(program_sheet, header_map[normalize_header(header)])
        for header in PROGRAM_REQUIRED_HEADERS
    )

    programs: list[dict[str, str]] = []
    errors: list[str] = []

    for row_number in range(DATA_START_ROW, last_row + 1):
        row = {
            header: text(
                get_cell_by_header(
                    program_sheet,
                    row_number,
                    header_map,
                    header,
                )
            )
            for header in PROGRAM_REQUIRED_HEADERS
        }
        if not any(row.values()):
            continue

        active = parse_optional_int(row["is_active"])
        missing = [
            name
            for name in (
                "program_name",
                "program_url",
                "is_active",
                "directory_name",
                "program_abbreviation",
            )
            if row[name] == ""
        ]
        if active not in (0, 1):
            missing.append("is_active(0-or-1)")
        if missing:
            errors.append(f"row={row_number} invalid={','.join(missing)}")
            continue
        if active == 0:
            continue

        row["_row"] = str(row_number)
        programs.append(row)

    if errors:
        raise RuntimeError("Invalid program rows: " + " | ".join(errors))
    if not programs:
        raise RuntimeError("The program worksheet contains no active programs.")

    LOGGER.info("Active program rows=%s", len(programs))
    return programs


def load_existing_recordings(
    recording_sheet: Any,
    header_map: dict[str, int],
) -> dict[str, ExistingRecording]:
    recording_id_column = header_map[normalize_header("recording_id")]
    status_column = header_map[normalize_header("status")]
    last_row = last_nonempty_row(recording_sheet, recording_id_column)

    result: dict[str, ExistingRecording] = {}
    duplicates: list[str] = []

    for row_number in range(DATA_START_ROW, last_row + 1):
        recording_id = text(recording_sheet.Cells(row_number, recording_id_column).Value2)
        if recording_id == "":
            continue
        if recording_id in result:
            duplicates.append(recording_id)
            continue
        result[recording_id] = ExistingRecording(
            row_number=row_number,
            status=parse_optional_int(
                recording_sheet.Cells(row_number, status_column).Value2
            ),
        )

    if duplicates:
        raise RuntimeError(
            "The recording worksheet contains duplicate recording_id values: "
            + repr(sorted(set(duplicates)))
        )

    LOGGER.info("Existing recording rows=%s", len(result))
    return result


def parse_program_url(program_url: str) -> tuple[str, str]:
    parsed = urlparse(program_url)
    values = parse_qs(parsed.query).get("p", [])
    if values:
        parameter = text(values[0])
        if "_" not in parameter:
            raise RuntimeError(f"Could not parse URL parameter p: {parameter}")
        site_id, corner_id = parameter.rsplit("_", 1)
        if site_id and corner_id:
            return site_id, corner_id

    match = re.search(
        r"/rs/(?P<site_id>[A-Za-z0-9]+)/?(?:list/?)?$",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if match:
        return text(match.group("site_id")), "01"

    raise RuntimeError(f"Unsupported NHK program URL: {program_url}")


def resolve_program_output_root(raw_value: str) -> Path:
    value = text(raw_value)
    if value == "":
        return AUDIO_ROOT_DIRECTORY.resolve()
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = BASE_DIRECTORY / path
    return path.resolve()


def sanitize_windows_name(value: str, fallback: str) -> str:
    result = re.sub(r'[\\/:*?"<>|]', "_", text(value))
    result = re.sub(r"[\x00-\x1f]", "", result)
    result = re.sub(r"\s+", " ", result).strip().rstrip(". ")
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if result.upper() in reserved:
        result = f"_{result}"
    return result or fallback


def fetch_program_data(
    program_url: str,
    site_id: str,
    corner_id: str,
) -> dict[str, Any]:
    api_url = (
        "https://www.nhk.or.jp/radio-api/app/v1/web/ondemand/series"
        f"?site_id={quote(site_id)}&corner_site_id={quote(corner_id)}"
    )
    headers = dict(HTTP_HEADERS)
    headers["Referer"] = program_url
    request = Request(api_url, headers=headers)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"NHK HTTP error: {error.code} {error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"NHK network error: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"NHK response parsing error: {error}") from error

    if not isinstance(payload, dict) or not isinstance(payload.get("episodes"), list):
        raise RuntimeError("NHK response has an unexpected structure.")

    LOGGER.info(
        "NHK metadata fetched: program=%s episodes=%s elapsed=%.3fs",
        text(payload.get("title")),
        len(payload["episodes"]),
        time.perf_counter() - started,
    )
    return payload


def infer_episode_date(episode: dict[str, Any]) -> datetime | None:
    sources = [
        text(episode.get("aa_contents_id")),
        text(episode.get("closed_at")),
        text(episode.get("onair_date")),
    ]
    for source in sources:
        compact = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", source)
        if compact:
            try:
                return datetime(
                    int(compact.group(1)),
                    int(compact.group(2)),
                    int(compact.group(3)),
                )
            except ValueError:
                pass

        separated = re.search(
            r"(?P<year>20\d{2})[-/年]"
            r"(?P<month>\d{1,2})[-/月]"
            r"(?P<day>\d{1,2})日?",
            source,
        )
        if separated:
            try:
                return datetime(
                    int(separated.group("year")),
                    int(separated.group("month")),
                    int(separated.group("day")),
                )
            except ValueError:
                pass

    onair_text = text(episode.get("onair_date"))
    month_day = re.search(
        r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日",
        onair_text,
    )
    if month_day:
        try:
            return datetime(
                datetime.now(JST).year,
                int(month_day.group("month")),
                int(month_day.group("day")),
            )
        except ValueError:
            pass
    return None


def infer_episode_onair_at(episode: dict[str, Any]) -> datetime | None:
    value = infer_episode_date(episode)
    if value is None:
        return None
    onair_text = text(episode.get("onair_date"))
    match = re.search(
        r"(?P<period>午前|午後)?(?P<hour>\d{1,2}):(?P<minute>\d{2})",
        onair_text,
    )
    if match is None:
        return value
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    period = text(match.group("period"))
    if period == "午前" and hour == 12:
        hour = 0
    elif period == "午後" and hour < 12:
        hour += 12
    return value.replace(hour=hour, minute=minute)


def make_episode_title(
    program_name: str,
    series_title: str,
    episode_title: str,
    episode_id: str,
) -> str:
    result = text(episode_title)
    prefixes = sorted(
        {text(program_name), text(series_title)},
        key=len,
        reverse=True,
    )
    for prefix in prefixes:
        if prefix and result.casefold().startswith(prefix.casefold()):
            result = result[len(prefix):]
            break
    result = re.sub(r"^[\s\u3000:\uff1a\-\u2013\u2014]+", "", result).strip()
    result = result.strip("「」『』\"'")
    result = re.sub(r"\s+", " ", result).strip()
    return result or f"episode_{episode_id}"


def build_audio_file_name(
    episode: dict[str, Any],
    title: str,
    episode_id: str,
) -> str:
    date_value = infer_episode_date(episode)
    date_text = date_value.strftime("%y%m%d") if date_value else "000000"
    safe_title = sanitize_windows_name(title, f"episode_{episode_id}")[:170]
    return f"{date_text}_{safe_title}.m4a"


def reserve_output_path(directory: Path, desired_name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    desired = directory / desired_name
    if not desired.exists():
        return desired
    for number in range(2, 10000):
        candidate = directory / f"{desired.stem}.#{number:02d}{desired.suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not reserve a file name: {desired}")


def make_player_url(site_id: str, corner_id: str, episode_id: str) -> str:
    return (
        "https://www.nhk.or.jp/radio/player/ondemand.html?p="
        f"{site_id}_{corner_id}_{episode_id}"
    )


def build_download_plan(
    programs: list[dict[str, str]],
    existing: dict[str, ExistingRecording],
) -> tuple[list[DownloadJob], int]:
    jobs: list[DownloadJob] = []
    skip_count = 0
    sequence = 0
    planned_ids: set[str] = set()

    for program in programs:
        site_id, corner_id = parse_program_url(program["program_url"])
        output_root = resolve_program_output_root(program["output_directory"])
        directory_name = sanitize_windows_name(
            program["directory_name"],
            program["program_abbreviation"],
        )
        output_directory = output_root / directory_name
        payload = fetch_program_data(
            program["program_url"],
            site_id,
            corner_id,
        )
        series_title = text(payload.get("title"))

        for episode in payload["episodes"]:
            if not isinstance(episode, dict):
                continue
            episode_id = text(episode.get("id"))
            if episode_id == "":
                raise RuntimeError(
                    f"NHK episode without id: program={program['program_name']}"
                )
            recording_id = f"{site_id}_{corner_id}_{episode_id}"
            if recording_id in planned_ids:
                raise RuntimeError(f"Duplicate recording_id in NHK response: {recording_id}")
            planned_ids.add(recording_id)

            current = existing.get(recording_id)
            if current is not None and current.status == 0:
                skip_count += 1
                LOGGER.info(
                    "[SKIP] recording_id=%s row=%s status=0",
                    recording_id,
                    current.row_number,
                )
                continue

            stream_url = text(episode.get("stream_url"))
            if stream_url == "":
                raise RuntimeError(f"stream_url is empty: recording_id={recording_id}")

            title = make_episode_title(
                program["program_name"],
                series_title,
                text(episode.get("program_title")),
                episode_id,
            )
            file_name = build_audio_file_name(episode, title, episode_id)
            output_path = reserve_output_path(output_directory, file_name)

            sequence += 1
            jobs.append(
                DownloadJob(
                    sequence=sequence,
                    target_row=current.row_number if current else None,
                    program_name=series_title or program["program_name"],
                    program_url=program["program_url"],
                    directory_name=directory_name,
                    output_root=output_root,
                    site_id=site_id,
                    corner_id=corner_id,
                    recording_id=recording_id,
                    episode_id=episode_id,
                    episode_title=title,
                    onair_at=infer_episode_onair_at(episode),
                    stream_url=stream_url,
                    player_url=make_player_url(site_id, corner_id, episode_id),
                    output_path=output_path,
                )
            )
            LOGGER.info(
                "[QUEUE] seq=%s recording_id=%s output=%s",
                sequence,
                recording_id,
                output_path,
            )

    LOGGER.info(
        "DOWNLOAD PLAN COMPLETE jobs=%s skip=%s",
        len(jobs),
        skip_count,
    )
    return jobs, skip_count


def resolve_media_tool(file_name: str) -> str:
    executable = file_name if file_name.lower().endswith(".exe") else f"{file_name}.exe"
    candidates = [
        BASE_DIRECTORY.parent.parent / "bin" / executable,
        SCRIPT_PATH.parent.parent.parent / "bin" / executable,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which(file_name) or shutil.which(executable)
    if found:
        return str(Path(found).resolve())
    raise RuntimeError(f"{file_name} was not found.")


def fetch_hls_text(url: str) -> str:
    headers = dict(HTTP_HEADERS)
    headers["Accept"] = "application/vnd.apple.mpegurl, text/plain, */*"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as error:
        raise RuntimeError(f"HLS HTTP error: {error.code} {error.reason}") from error
    except URLError as error:
        raise RuntimeError(f"HLS network error: {error.reason}") from error
    for encoding in ("utf-8-sig", "utf-8", "shift_jis"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not decode HLS playlist: {url}")


def parse_hls_attribute_list(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', raw, flags=re.I):
        result[match.group(1).upper()] = match.group(2).strip().strip('"')
    return result


def inspect_hls_manifest(stream_url: str, max_depth: int = 4) -> HlsManifestInfo:
    current_url = stream_url
    visited: set[str] = set()

    for _ in range(max_depth + 1):
        if current_url in visited:
            raise RuntimeError(f"HLS playlist loop detected: {current_url}")
        visited.add(current_url)

        lines = [
            line.strip()
            for line in fetch_hls_text(current_url).splitlines()
            if line.strip()
        ]
        if not lines or lines[0] != "#EXTM3U":
            raise RuntimeError(f"Invalid HLS playlist: {current_url}")

        variants: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            if not line.upper().startswith("#EXT-X-STREAM-INF:"):
                continue
            attributes = parse_hls_attribute_list(line.split(":", 1)[1])
            try:
                bandwidth = int(attributes.get("BANDWIDTH", "0"))
            except ValueError:
                bandwidth = 0
            for candidate in lines[index + 1:]:
                if not candidate.startswith("#"):
                    variants.append((bandwidth, urljoin(current_url, candidate)))
                    break

        if variants:
            variants.sort(key=lambda value: value[0], reverse=True)
            current_url = variants[0][1]
            continue

        durations: list[float] = []
        segment_count = 0
        waiting_for_segment = False
        for line in lines:
            if line.upper().startswith("#EXTINF:"):
                durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
                waiting_for_segment = True
            elif waiting_for_segment and not line.startswith("#"):
                segment_count += 1
                waiting_for_segment = False

        if not durations or segment_count != len(durations):
            raise RuntimeError(f"Invalid HLS media playlist: {current_url}")

        return HlsManifestInfo(
            media_playlist_url=current_url,
            expected_duration_seconds=sum(durations),
            segment_count=segment_count,
            has_endlist=any(line.upper() == "#EXT-X-ENDLIST" for line in lines),
        )

    raise RuntimeError(f"HLS playlist nesting exceeds {max_depth}: {stream_url}")


def probe_media_duration(ffprobe_path: str, source: Path) -> float:
    completed = run_child_process(
        [
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(source),
        ],
        FFPROBE_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffprobe failed")
    try:
        value = float(json.loads(completed.stdout)["format"]["duration"])
    except Exception as error:
        raise RuntimeError(f"Could not parse ffprobe output: {completed.stdout!r}") from error
    if value <= 0:
        raise RuntimeError(f"Invalid audio duration: {value}")
    return value


def validate_audio_decode(ffmpeg_path: str, media_path: Path) -> None:
    completed = run_child_process(
        [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-xerror",
            "-err_detect", "explode",
            "-i", str(media_path),
            "-map", "0:a:0",
            "-f", "null",
            os.devnull,
        ],
        FFMPEG_TIMEOUT_SECONDS,
    )
    error_text = (completed.stderr or "").strip()
    if completed.returncode != 0 or error_text:
        raise RuntimeError(
            f"Strict audio decode validation failed: {error_text or completed.returncode}"
        )


def calculate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_duration(expected: float, actual: float) -> None:
    tolerance = max(DURATION_TOLERANCE_MIN_SECONDS, expected * DURATION_TOLERANCE_RATIO)
    if actual < max(1.0, expected - tolerance) or actual > expected + tolerance:
        raise RuntimeError(
            f"Audio duration is outside tolerance: expected={expected:.3f} "
            f"actual={actual:.3f} tolerance={tolerance:.3f}"
        )


def download_one(
    ffmpeg_path: str,
    ffprobe_path: str,
    job: DownloadJob,
) -> DownloadResult:
    started = time.perf_counter()
    safe_id = re.sub(r"[^0-9A-Za-z._-]+", "_", job.recording_id)
    work_directory = WORK_ROOT_DIRECTORY / RUN_ID / safe_id
    shutil.rmtree(work_directory, ignore_errors=True)
    work_directory.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    try:
        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            if CANCEL_EVENT.is_set():
                raise RuntimeError("Download cancelled.")

            temporary = work_directory / f"attempt_{attempt:02d}.m4a"
            temporary.unlink(missing_ok=True)
            try:
                manifest = inspect_hls_manifest(job.stream_url)
                if not manifest.has_endlist:
                    raise RuntimeError("HLS playlist does not contain EXT-X-ENDLIST.")

                command = [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel", "error",
                    "-nostdin",
                    "-xerror",
                    "-err_detect", "explode",
                    "-y",
                    "-http_seekable", "0",
                    "-i", job.stream_url,
                    "-vn",
                    "-c:a", "copy",
                    "-bsf:a", "aac_adtstoasc",
                    str(temporary),
                ]

                LOGGER.info(
                    "[DOWNLOAD START] seq=%s recording_id=%s attempt=%s/%s",
                    job.sequence,
                    job.recording_id,
                    attempt,
                    MAX_DOWNLOAD_ATTEMPTS,
                )
                completed = run_child_process(command, FFMPEG_TIMEOUT_SECONDS)
                error_text = (completed.stderr or "").strip()
                if completed.returncode != 0 or error_text:
                    raise RuntimeError(
                        f"FFmpeg failed: {error_text or completed.returncode}"
                    )
                if not temporary.is_file() or temporary.stat().st_size <= 0:
                    raise RuntimeError("FFmpeg did not create a valid audio file.")

                actual_duration = probe_media_duration(ffprobe_path, temporary)
                validate_duration(manifest.expected_duration_seconds, actual_duration)
                validate_audio_decode(ffmpeg_path, temporary)
                sha256_value = calculate_sha256(temporary)

                final_path = job.output_path
                if final_path.exists():
                    final_path = reserve_output_path(final_path.parent, final_path.name)
                    job.output_path = final_path
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, final_path)

                LOGGER.info(
                    "[DOWNLOAD PASS] seq=%s recording_id=%s file=%s "
                    "size=%s expected=%.3f actual=%.3f sha256=%s elapsed=%.3fs",
                    job.sequence,
                    job.recording_id,
                    final_path,
                    final_path.stat().st_size,
                    manifest.expected_duration_seconds,
                    actual_duration,
                    sha256_value,
                    time.perf_counter() - started,
                )

                return DownloadResult(
                    job=job,
                    success=True,
                    file_size_bytes=final_path.stat().st_size,
                    expected_duration_seconds=manifest.expected_duration_seconds,
                    actual_duration_seconds=actual_duration,
                    sha256=sha256_value,
                    completed_at=jst_now_naive(),
                    error_message="",
                    attempts=attempt,
                )
            except KeyboardInterrupt:
                raise
            except Exception as error:
                temporary.unlink(missing_ok=True)
                errors.append(f"attempt {attempt}: {text(error)}")
                LOGGER.exception(
                    "[DOWNLOAD FAILED] seq=%s recording_id=%s attempt=%s/%s reason=%s",
                    job.sequence,
                    job.recording_id,
                    attempt,
                    MAX_DOWNLOAD_ATTEMPTS,
                    error,
                )
                if attempt < MAX_DOWNLOAD_ATTEMPTS:
                    time.sleep(min(2 ** (attempt - 1), 4))

        return DownloadResult(
            job=job,
            success=False,
            file_size_bytes=None,
            expected_duration_seconds=None,
            actual_duration_seconds=None,
            sha256="",
            completed_at=jst_now_naive(),
            error_message=" | ".join(errors),
            attempts=MAX_DOWNLOAD_ATTEMPTS,
        )
    finally:
        shutil.rmtree(work_directory, ignore_errors=True)


def run_parallel_downloads(
    jobs: list[DownloadJob],
    ffmpeg_path: str,
    ffprobe_path: str,
) -> list[DownloadResult]:
    if not jobs:
        return []

    worker_count = min(MAX_CONCURRENT_DOWNLOADS, len(jobs))
    results: list[DownloadResult] = []
    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="nhk-ffmpeg",
    )
    future_to_job: dict[Future[DownloadResult], DownloadJob] = {
        executor.submit(download_one, ffmpeg_path, ffprobe_path, job): job
        for job in jobs
    }

    try:
        pending: set[Future[DownloadResult]] = set(future_to_job)
        while pending:
            done, pending = wait(
                pending,
                timeout=0.5,
                return_when=FIRST_COMPLETED,
            )
            if CANCEL_EVENT.is_set():
                raise KeyboardInterrupt
            for future in done:
                job = future_to_job[future]
                try:
                    results.append(future.result())
                except Exception as error:
                    LOGGER.exception(
                        "[WORKER ERROR] recording_id=%s reason=%s",
                        job.recording_id,
                        error,
                    )
                    results.append(
                        DownloadResult(
                            job=job,
                            success=False,
                            file_size_bytes=None,
                            expected_duration_seconds=None,
                            actual_duration_seconds=None,
                            sha256="",
                            completed_at=jst_now_naive(),
                            error_message=f"Unhandled worker error: {error}",
                            attempts=MAX_DOWNLOAD_ATTEMPTS,
                        )
                    )
    except KeyboardInterrupt:
        CANCEL_EVENT.set()
        terminate_all_child_processes()
        for future in future_to_job:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=CANCEL_EVENT.is_set())

    return sorted(results, key=lambda item: item.job.sequence)


def find_recording_row(
    sheet: Any,
    header_map: dict[str, int],
    recording_id: str,
) -> int | None:
    column = header_map[normalize_header("recording_id")]
    last_row = last_nonempty_row(sheet, column)
    wanted = recording_id.casefold()
    for row_number in range(DATA_START_ROW, last_row + 1):
        current = text(sheet.Cells(row_number, column).Value2)
        if current.casefold() == wanted:
            return row_number
    return None


def result_values(result: DownloadResult) -> dict[str, Any]:
    job = result.job
    return {
        "program_name": job.program_name,
        "onair_at": excel_serial(job.onair_at),
        "status": 0 if result.success else 1,
        "episode_title": job.episode_title,
        "actual_duration_seconds": (
            result.actual_duration_seconds if result.success else None
        ),
        "downloaded_at": (
            excel_serial(result.completed_at) if result.success else None
        ),
        "file_name": job.output_path.name,
        "description": "" if result.success else result.error_message,
        "file_size": bytes_to_mib(result.file_size_bytes) if result.success else None,
        "saved_directory": str(job.output_path.resolve().parent),
        "recording_id": job.recording_id,
        "player_url": job.player_url,
    }


def write_result_row(
    sheet: Any,
    row_number: int,
    header_map: dict[str, int],
    values: dict[str, Any],
) -> None:
    for header_name, value in values.items():
        normalized = normalize_header(header_name)
        if normalized not in header_map:
            continue
        cell = sheet.Cells(row_number, header_map[normalized])
        if header_name in {
            "program_name", "episode_title", "file_name", "description",
            "saved_directory", "recording_id", "player_url",
        }:
            cell.NumberFormat = "@"
        elif header_name == "status":
            cell.NumberFormat = "0"
        elif header_name in {"actual_duration_seconds", "file_size"}:
            cell.NumberFormat = "0.000"
        elif header_name in {"onair_at", "downloaded_at"}:
            cell.NumberFormat = "yyyy/mm/dd hh:mm:ss"
        cell.Value2 = value


def verify_result_row(
    sheet: Any,
    row_number: int,
    header_map: dict[str, int],
    result: DownloadResult,
) -> None:
    expected = {
        "recording_id": result.job.recording_id,
        "status": 0 if result.success else 1,
        "file_name": result.job.output_path.name,
        "saved_directory": str(result.job.output_path.resolve().parent),
    }
    mismatches: list[str] = []
    for header, expected_value in expected.items():
        actual = sheet.Cells(row_number, header_map[normalize_header(header)]).Value2
        if header == "status":
            matched = parse_optional_int(actual) == expected_value
        else:
            matched = text(actual) == text(expected_value)
        if not matched:
            mismatches.append(
                f"{header}: expected={expected_value!r} actual={actual!r}"
            )
    if mismatches:
        raise RuntimeError("Excel readback verification failed: " + "; ".join(mismatches))


def ensure_excel_writable(excel: Any, workbook: Any, sheet: Any) -> None:
    if bool(workbook.ReadOnly):
        raise RuntimeError("The Excel workbook is read-only.")
    if bool(sheet.ProtectContents):
        raise RuntimeError("The recording worksheet is protected.")
    if not bool(excel.Ready):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.2)
            if bool(excel.Ready):
                break
        else:
            raise RuntimeError("Excel did not become ready for writing.")


def commit_results_to_excel(
    results: list[DownloadResult],
    pywintypes: Any,
    win32_client: Any,
    pythoncom: Any,
) -> tuple[int, int, Any, Any]:
    committed = 0
    pending = 0
    excel = None
    workbook = None

    for index, result in enumerate(results, start=1):
        last_error: Exception | None = None
        for attempt in range(1, MAX_EXCEL_WRITE_ATTEMPTS + 1):
            try:
                excel, workbook, _opened = connect_or_open_workbook(
                    pywintypes,
                    win32_client,
                    pythoncom,
                )
                recording_sheet = get_sheet(workbook, RECORDING_SHEET_NAME)
                if recording_sheet is None:
                    raise RuntimeError("The recording worksheet does not exist.")
                header_map, _ = read_header_map(recording_sheet)
                require_headers(
                    RECORDING_SHEET_NAME,
                    header_map,
                    RECORDING_REQUIRED_HEADERS,
                )
                ensure_excel_writable(excel, workbook, recording_sheet)

                row_number = find_recording_row(
                    recording_sheet,
                    header_map,
                    result.job.recording_id,
                )
                if row_number is None:
                    row_number = max(
                        DATA_START_ROW,
                        last_occupied_row(recording_sheet, header_map) + 1,
                    )

                LOGGER.info(
                    "[EXCEL WRITE START] item=%s/%s attempt=%s/%s "
                    "recording_id=%s row=%s status=%s",
                    index,
                    len(results),
                    attempt,
                    MAX_EXCEL_WRITE_ATTEMPTS,
                    result.job.recording_id,
                    row_number,
                    0 if result.success else 1,
                )

                write_result_row(
                    recording_sheet,
                    row_number,
                    header_map,
                    result_values(result),
                )
                pythoncom.PumpWaitingMessages()
                verify_result_row(
                    recording_sheet,
                    row_number,
                    header_map,
                    result,
                )

                # Explicit Save is the authority. AutoSave is never read as a gate.
                workbook.Save()
                pythoncom.PumpWaitingMessages()

                LOGGER.info(
                    "[EXCEL WRITE PASS] item=%s/%s recording_id=%s row=%s "
                    "explicit_save=PASS autosave_required=False",
                    index,
                    len(results),
                    result.job.recording_id,
                    row_number,
                )
                committed += 1
                break
            except Exception as error:
                last_error = error
                LOGGER.exception(
                    "[EXCEL WRITE FAILED] item=%s/%s attempt=%s/%s "
                    "recording_id=%s reason=%s",
                    index,
                    len(results),
                    attempt,
                    MAX_EXCEL_WRITE_ATTEMPTS,
                    result.job.recording_id,
                    error,
                )
                if attempt < MAX_EXCEL_WRITE_ATTEMPTS:
                    time.sleep(EXCEL_WRITE_RETRY_DELAYS_SECONDS[attempt - 1])
        else:
            pending += 1
            LOGGER.error(
                "[EXCEL RESULT PENDING] recording_id=%s reason=%s",
                result.job.recording_id,
                last_error,
            )

    return committed, pending, excel, workbook


def create_validation_package(summary: dict[str, Any]) -> None:
    RUN_SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with zipfile.ZipFile(
        VALIDATION_ZIP_PATH,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        if RUN_LOG_PATH.exists():
            archive.write(RUN_LOG_PATH, RUN_LOG_PATH.name)
        archive.write(RUN_SUMMARY_PATH, RUN_SUMMARY_PATH.name)


def main() -> int:
    started = time.perf_counter()
    exit_code = 1
    pythoncom = None
    excel = None
    workbook = None
    original_status_bar: Any = False
    jobs: list[DownloadJob] = []
    results: list[DownloadResult] = []
    skip_count = 0
    committed_count = 0
    pending_count = 0

    LOGGER.info("================ RUN START ================")
    LOGGER.info("VERSION=%s", PROGRAM_VERSION)
    LOGGER.info("SCRIPT=%s", SCRIPT_PATH)
    LOGGER.info("CWD=%s", BASE_DIRECTORY)
    LOGGER.info("WORKBOOK_PATH=%s", WORKBOOK_PATH)
    LOGGER.info("HOST=%s USER=%s PLATFORM=%s", socket.gethostname(), getpass.getuser(), platform.platform())
    LOGGER.info("PYTHON=%s", sys.version.replace("\n", " "))
    LOGGER.info("MAX_CONCURRENT_DOWNLOADS=%s", MAX_CONCURRENT_DOWNLOADS)
    LOGGER.info("AUTOSAVE_POLICY=diagnostic-only; never blocks download; never changed by program")
    LOGGER.info("EXCEL_SAVE_POLICY=workbook.Save after every verified recording row")
    LOGGER.info("DOWNLOAD_DECISION=recording status=0 skip; all other states download")

    try:
        acquire_single_instance_lock()
        install_interrupt_handlers()
        pythoncom, pywintypes, win32_client = import_excel_modules()
        pythoncom.CoInitialize()

        excel, workbook, opened_by_program = connect_or_open_workbook(
            pywintypes,
            win32_client,
            pythoncom,
        )
        original_status_bar = excel.StatusBar
        program_sheet = get_sheet(workbook, PROGRAM_SHEET_NAME)
        recording_sheet = get_sheet(workbook, RECORDING_SHEET_NAME)
        if program_sheet is None:
            raise RuntimeError("The program worksheet does not exist.")
        if recording_sheet is None:
            raise RuntimeError("The recording worksheet does not exist.")

        program_header_map, _ = read_header_map(program_sheet)
        require_headers(PROGRAM_SHEET_NAME, program_header_map, PROGRAM_REQUIRED_HEADERS)
        recording_header_map, _ = read_header_map(recording_sheet)
        require_headers(
            RECORDING_SHEET_NAME,
            recording_header_map,
            RECORDING_REQUIRED_HEADERS,
        )
        ensure_excel_writable(excel, workbook, recording_sheet)

        excel.ScreenUpdating = True
        excel.StatusBar = "NHK downloader: reading Excel and planning downloads"
        pythoncom.PumpWaitingMessages()

        programs = read_program_rows(program_sheet)
        existing = load_existing_recordings(
            recording_sheet,
            recording_header_map,
        )
        jobs, skip_count = build_download_plan(programs, existing)

        ffmpeg_path = resolve_media_tool("ffmpeg")
        ffprobe_path = resolve_media_tool("ffprobe")
        LOGGER.info("FFMPEG=%s", ffmpeg_path)
        LOGGER.info("FFPROBE=%s", ffprobe_path)

        excel.StatusBar = "NHK downloader: downloading audio"
        pythoncom.PumpWaitingMessages()
        results = run_parallel_downloads(jobs, ffmpeg_path, ffprobe_path)

        success_count = sum(1 for result in results if result.success)
        download_error_count = len(results) - success_count
        LOGGER.info(
            "ALL DOWNLOADS ENDED results=%s success=%s error=%s",
            len(results),
            success_count,
            download_error_count,
        )

        excel.StatusBar = "NHK downloader: writing results and saving Excel"
        pythoncom.PumpWaitingMessages()
        committed_count, pending_count, excel, workbook = commit_results_to_excel(
            results,
            pywintypes,
            win32_client,
            pythoncom,
        )

        total_errors = download_error_count + pending_count
        LOGGER.info(
            "RUN RESULT success=%s download_error=%s excel_committed=%s "
            "pending_excel=%s skip=%s",
            success_count,
            download_error_count,
            committed_count,
            pending_count,
            skip_count,
        )
        exit_code = 0 if total_errors == 0 else 1
        return exit_code

    except KeyboardInterrupt:
        CANCEL_EVENT.set()
        terminate_all_child_processes()
        LOGGER.warning("[CANCELLED] Ctrl-C stopped the run.")
        exit_code = 130
        return exit_code
    except Exception as error:
        LOGGER.exception("[FATAL] %s", error)
        exit_code = 1
        return exit_code
    finally:
        terminate_all_child_processes()
        shutil.rmtree(WORK_ROOT_DIRECTORY / RUN_ID, ignore_errors=True)

        if excel is not None:
            try:
                excel.ScreenUpdating = True
                excel.StatusBar = original_status_bar if original_status_bar not in (None, "") else False
                if pythoncom is not None:
                    pythoncom.PumpWaitingMessages()
                LOGGER.info("EXCEL_LEFT_OPEN=True")
            except Exception:
                LOGGER.exception("Failed to restore Excel state.")

        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                LOGGER.exception("CoUninitialize failed.")

        release_single_instance_lock()
        ended_at = datetime.now(JST)
        elapsed = time.perf_counter() - started
        LOGGER.info(
            "RUN END exit_code=%s elapsed=%.3fs ended_at=%s",
            exit_code,
            elapsed,
            ended_at.isoformat(),
        )
        LOGGER.info("UPLOAD_FOR_VALIDATION=%s", VALIDATION_ZIP_PATH)
        LOGGER.info("================= RUN END =================")
        flush_logs()

        summary = {
            "program_version": PROGRAM_VERSION,
            "run_id": RUN_ID,
            "started_at": RUN_STARTED_AT_JST.isoformat(),
            "ended_at": ended_at.isoformat(),
            "elapsed_seconds": round(elapsed, 3),
            "exit_code": exit_code,
            "planned_download_jobs": len(jobs),
            "result_count": len(results),
            "success_count": sum(1 for result in results if result.success),
            "download_error_count": sum(1 for result in results if not result.success),
            "excel_committed_count": committed_count,
            "pending_excel_count": pending_count,
            "skip_count": skip_count,
            "workbook": str(WORKBOOK_PATH),
            "daily_log": str(LOG_PATH),
            "run_log": str(RUN_LOG_PATH),
        }
        try:
            create_validation_package(summary)
            print(f"VALIDATION PACKAGE : {VALIDATION_ZIP_PATH}", flush=True)
        except Exception as package_error:
            print(
                f"VALIDATION PACKAGE ERROR: {package_error}",
                file=sys.stderr,
                flush=True,
            )


def guarded_entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        CANCEL_EVENT.set()
        terminate_all_child_processes()
        flush_logs()
        return 130
    except Exception as error:
        LOGGER.critical(
            "UNHANDLED_EXCEPTION type=%s message=%s\n%s",
            type(error).__name__,
            error,
            traceback.format_exc(),
        )
        flush_logs()
        return 99


if __name__ == "__main__":
    raise SystemExit(guarded_entrypoint())
