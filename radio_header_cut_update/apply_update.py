from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


STATUS_ROWS = [
    (0, "completed", "ダウンロードと先頭カットがすべて正常に終了した", "none", 1, 0),
    (10, "pending_download", "ダウンロード待ち", "download", 0, 0),
    (20, "downloading", "ダウンロード中", "wait", 0, 0),
    (30, "download_failed", "ダウンロードに失敗した", "download", 0, 1),
    (40, "pending_conversion", "ダウンロードは完了したが先頭カットは未実行", "convert", 0, 0),
    (50, "converting", "先頭カット処理中", "wait", 0, 0),
    (60, "conversion_failed", "ダウンロードは成功したが先頭カットに失敗した。再ダウンロードは行わない", "convert", 0, 1),
]

PROGRAM_COLUMNS = {
    "header_cut_default_seconds": "INTEGER NOT NULL DEFAULT 0 CHECK (header_cut_default_seconds >= 0)",
    "header_cut_mon_seconds": "INTEGER CHECK (header_cut_mon_seconds IS NULL OR header_cut_mon_seconds >= 0)",
    "header_cut_tue_seconds": "INTEGER CHECK (header_cut_tue_seconds IS NULL OR header_cut_tue_seconds >= 0)",
    "header_cut_wed_seconds": "INTEGER CHECK (header_cut_wed_seconds IS NULL OR header_cut_wed_seconds >= 0)",
    "header_cut_thu_seconds": "INTEGER CHECK (header_cut_thu_seconds IS NULL OR header_cut_thu_seconds >= 0)",
    "header_cut_fri_seconds": "INTEGER CHECK (header_cut_fri_seconds IS NULL OR header_cut_fri_seconds >= 0)",
}

RECORDING_COLUMNS = {
    "status_code": "INTEGER NOT NULL DEFAULT 10",
    "original_file_path": "TEXT NOT NULL DEFAULT ''",
    "converted_file_path": "TEXT NOT NULL DEFAULT ''",
    "applied_cut_seconds": "INTEGER CHECK (applied_cut_seconds IS NULL OR applied_cut_seconds >= 0)",
    "converted_file_size_bytes": "INTEGER NOT NULL DEFAULT 0 CHECK (converted_file_size_bytes >= 0)",
    "converted_duration_seconds": "REAL",
    "converted_sha256": "TEXT NOT NULL DEFAULT ''",
    "conversion_started_at": "TEXT",
    "conversion_completed_at": "TEXT",
    "status_description": "TEXT NOT NULL DEFAULT ''",
}

MARKER = "HEADER_CUT_WRAPPER_V1"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({q(table_name)})")}


def add_missing_columns(
    conn: sqlite3.Connection,
    table_name: str,
    definitions: dict[str, str],
    log: list[str],
) -> None:
    existing = columns(conn, table_name)
    for name, definition in definitions.items():
        if name in existing:
            log.append(f"EXISTS_COLUMN={table_name}.{name}")
            continue
        conn.execute(
            f"ALTER TABLE {q(table_name)} ADD COLUMN {q(name)} {definition}"
        )
        existing.add(name)
        log.append(f"ADDED_COLUMN={table_name}.{name}")


def ensure_status_master(conn: sqlite3.Connection, log: list[str]) -> None:
    required = {
        "status_code",
        "status_name",
        "status_description",
        "next_action",
        "is_completed",
        "is_error",
    }
    if table_exists(conn, "recording_status_master"):
        current = columns(conn, "recording_status_master")
        if not required.issubset(current):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            old_name = f"recording_status_master_incomplete_{stamp}"
            conn.execute(
                f"ALTER TABLE recording_status_master RENAME TO {q(old_name)}"
            )
            log.append(f"RENAMED_INCOMPLETE_TABLE={old_name}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recording_status_master (
            status_code INTEGER PRIMARY KEY,
            status_name TEXT NOT NULL UNIQUE,
            status_description TEXT NOT NULL,
            next_action TEXT NOT NULL,
            is_completed INTEGER NOT NULL DEFAULT 0 CHECK (is_completed IN (0,1)),
            is_error INTEGER NOT NULL DEFAULT 0 CHECK (is_error IN (0,1))
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO recording_status_master(
            status_code,
            status_name,
            status_description,
            next_action,
            is_completed,
            is_error
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(status_code) DO UPDATE SET
            status_name=excluded.status_name,
            status_description=excluded.status_description,
            next_action=excluded.next_action,
            is_completed=excluded.is_completed,
            is_error=excluded.is_error
        """,
        STATUS_ROWS,
    )
    log.append("STATUS_MASTER_ROWS=7")


def migrate_values(conn: sqlite3.Connection, log: list[str]) -> None:
    conn.execute(
        """
        UPDATE programs
        SET
            header_cut_default_seconds=53,
            header_cut_fri_seconds=53
        WHERE program_name='ラジオ英会話'
        """
    )
    log.append("RADIO_ENGLISH_DEFAULT_SECONDS=53")
    log.append("RADIO_ENGLISH_FRIDAY_SECONDS=53")

    rcols = columns(conn, "recordings")
    if {"status_code", "download_status"}.issubset(rcols):
        conn.execute(
            """
            UPDATE recordings
            SET status_code=CASE download_status
                WHEN 'downloaded'  THEN 40
                WHEN 'validated'   THEN 40
                WHEN 'failed'      THEN 30
                WHEN 'expired'     THEN 30
                WHEN 'downloading' THEN 20
                WHEN 'queued'      THEN 10
                ELSE 10
            END
            WHERE status_code IS NULL
               OR status_code NOT IN (0,10,20,30,40,50,60)
               OR (status_code=10 AND download_status IN (
                    'downloaded','validated','failed','expired','downloading'
               ))
            """
        )
        log.append("MIGRATED_STATUS_CODE=YES")

    if {"original_file_path", "saved_directory", "file_name"}.issubset(rcols):
        conn.execute(
            """
            UPDATE recordings
            SET original_file_path=CASE
                WHEN saved_directory<>'' AND file_name<>''
                THEN rtrim(saved_directory, '\\/') || '\\' || file_name
                ELSE ''
            END
            WHERE original_file_path=''
            """
        )
        log.append("MIGRATED_ORIGINAL_PATH=YES")

    if {"status_description", "download_status", "last_error"}.issubset(rcols):
        conn.execute(
            """
            UPDATE recordings
            SET status_description=CASE
                WHEN download_status IN ('downloaded','validated')
                    THEN 'ダウンロード済み。先頭カット待ち'
                WHEN download_status='failed'
                    THEN COALESCE(last_error,'')
                WHEN download_status='expired'
                    THEN '配信期限切れ'
                ELSE status_description
            END
            WHERE status_description=''
            """
        )
        log.append("MIGRATED_STATUS_DESCRIPTION=YES")


def recreate_triggers(conn: sqlite3.Connection, log: list[str]) -> None:
    for name in (
        "trg_recording_status_insert",
        "trg_recording_status_update",
        "trg_recording_status_from_download_insert",
        "trg_recording_status_from_download_update",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {q(name)}")

    conn.executescript(
        """
        CREATE TRIGGER trg_recording_status_insert
        BEFORE INSERT ON recordings
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM recording_status_master
            WHERE status_code=NEW.status_code
        )
        BEGIN
            SELECT RAISE(ABORT, 'Invalid recordings.status_code');
        END;

        CREATE TRIGGER trg_recording_status_update
        BEFORE UPDATE OF status_code ON recordings
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM recording_status_master
            WHERE status_code=NEW.status_code
        )
        BEGIN
            SELECT RAISE(ABORT, 'Invalid recordings.status_code');
        END;

        CREATE TRIGGER trg_recording_status_from_download_insert
        AFTER INSERT ON recordings
        FOR EACH ROW
        WHEN NEW.status_code NOT IN (0,50,60)
        BEGIN
            UPDATE recordings
            SET status_code=CASE NEW.download_status
                WHEN 'downloaded'  THEN 40
                WHEN 'validated'   THEN 40
                WHEN 'failed'      THEN 30
                WHEN 'expired'     THEN 30
                WHEN 'downloading' THEN 20
                WHEN 'queued'      THEN 10
                ELSE 10
            END
            WHERE recording_id=NEW.recording_id;
        END;

        CREATE TRIGGER trg_recording_status_from_download_update
        AFTER UPDATE OF download_status ON recordings
        FOR EACH ROW
        WHEN NEW.status_code NOT IN (0,50,60)
        BEGIN
            UPDATE recordings
            SET status_code=CASE NEW.download_status
                WHEN 'downloaded'  THEN 40
                WHEN 'validated'   THEN 40
                WHEN 'failed'      THEN 30
                WHEN 'expired'     THEN 30
                WHEN 'downloading' THEN 20
                WHEN 'queued'      THEN 10
                ELSE 10
            END
            WHERE recording_id=NEW.recording_id;
        END;
        """
    )
    log.append("RECREATED_STATUS_TRIGGERS=YES")


def recreate_views(conn: sqlite3.Connection, log: list[str]) -> None:
    conn.execute("DROP VIEW IF EXISTS v_program")
    conn.execute("DROP VIEW IF EXISTS v_recording")

    pcols = columns(conn, "programs")
    required_p = {
        "program_id", "program_name", "program_url", "is_active",
        "output_directory", "directory_name", "program_abbreviation",
        "site_id", "corner_id", "header_cut_default_seconds",
        "header_cut_mon_seconds", "header_cut_tue_seconds",
        "header_cut_wed_seconds", "header_cut_thu_seconds",
        "header_cut_fri_seconds", "created_at", "updated_at",
    }
    if required_p.issubset(pcols):
        conn.execute(
            """
            CREATE VIEW v_program AS
            SELECT
                program_id, program_name, program_url, is_active,
                output_directory, directory_name, program_abbreviation,
                site_id, corner_id,
                header_cut_default_seconds,
                header_cut_mon_seconds,
                header_cut_tue_seconds,
                header_cut_wed_seconds,
                header_cut_thu_seconds,
                header_cut_fri_seconds,
                created_at, updated_at
            FROM programs
            """
        )
        log.append("VIEW_V_PROGRAM=CREATED")

    rcols = columns(conn, "recordings")
    ecols = columns(conn, "episodes")
    required_r = {
        "recording_id", "status_code", "download_status",
        "applied_cut_seconds", "actual_duration_seconds",
        "converted_duration_seconds", "downloaded_at",
        "conversion_completed_at", "file_name",
        "original_file_path", "converted_file_path",
        "file_size_bytes", "converted_file_size_bytes",
        "saved_directory", "expected_duration_seconds", "segment_count",
        "sha256", "converted_sha256", "attempt_count", "last_error",
        "status_description", "last_run_id",
    }
    required_e = {
        "recording_id", "program_id", "onair_at", "display_title",
        "episode_title", "description", "player_url", "closed_at",
        "availability_status",
    }
    if required_p.issubset(pcols) and required_r.issubset(rcols) and required_e.issubset(ecols):
        conn.execute(
            """
            CREATE VIEW v_recording AS
            SELECT
                e.recording_id,
                p.program_name,
                e.onair_at,
                r.status_code AS status,
                sm.status_name,
                sm.status_description AS status_master_description,
                sm.next_action,
                r.download_status,
                COALESCE(NULLIF(e.display_title,''),e.episode_title) AS episode_title,
                r.applied_cut_seconds,
                r.actual_duration_seconds,
                r.converted_duration_seconds,
                r.downloaded_at,
                r.conversion_completed_at,
                r.file_name,
                r.original_file_path,
                r.converted_file_path,
                CASE WHEN sm.is_error=1 THEN r.status_description
                     ELSE e.description END AS description,
                ROUND(r.file_size_bytes/1048576.0,3) AS original_file_size_mb,
                ROUND(r.converted_file_size_bytes/1048576.0,3) AS converted_file_size_mb,
                r.file_size_bytes,
                r.converted_file_size_bytes,
                r.saved_directory,
                e.player_url,
                e.closed_at,
                e.availability_status,
                r.expected_duration_seconds,
                r.segment_count,
                r.sha256,
                r.converted_sha256,
                r.attempt_count,
                r.last_error,
                r.status_description,
                r.last_run_id,
                p.program_id
            FROM episodes e
            JOIN programs p ON p.program_id=e.program_id
            LEFT JOIN recordings r ON r.recording_id=e.recording_id
            LEFT JOIN recording_status_master sm ON sm.status_code=r.status_code
            """
        )
        log.append("VIEW_V_RECORDING=CREATED")
    else:
        log.append("VIEW_V_RECORDING=SKIPPED_MISSING_BASE_COLUMNS")


def ensure_meta(conn: sqlite3.Connection, log: list[str]) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_meta(
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO app_meta(meta_key,meta_value) VALUES (?,?)
        ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value
        """,
        [
            ("schema_version", "4"),
            ("header_cut_enabled", "1"),
            ("header_cut_source_directory", "audio"),
            ("header_cut_output_directory", "audio2"),
            ("normal_status_code", "0"),
        ],
    )
    log.append("APP_META=UPDATED")


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


def install_files(package_dir: Path, root: Path, log: list[str]) -> None:
    for name in (
        "header_cut_processor.py",
        "CONVERT_PENDING.bat",
        "RETRY_CONVERT_FAILED.bat",
        "VERIFY_HEADER_CUT.bat",
    ):
        source = package_dir / name
        if not source.is_file():
            raise FileNotFoundError(source)
        shutil.copy2(source, root / name)
        log.append(f"INSTALLED_FILE={root / name}")

    current_run = root / "RUN_ALL.bat"
    download_only = root / "RUN_DOWNLOAD_ONLY.bat"
    original_backup = root / "RUN_ALL.before_header_cut.bat"
    wrapper = package_dir / "RUN_ALL_HEADER_CUT.bat"

    if current_run.is_file():
        text = current_run.read_text(encoding="utf-8-sig", errors="replace")
        if MARKER not in text:
            if not original_backup.exists():
                shutil.copy2(current_run, original_backup)
                log.append(f"BACKUP_FILE={original_backup}")
            no_pause = "\r\n".join(
                line for line in text.splitlines()
                if line.strip().lower() != "pause"
            ) + "\r\n"
            download_only.write_text(no_pause, encoding="utf-8-sig")
            log.append(f"CREATED_FILE={download_only}")
    elif not download_only.is_file():
        raise FileNotFoundError("RUN_ALL.bat が見つかりません")

    shutil.copy2(wrapper, current_run)
    log.append(f"INSTALLED_FILE={current_run}")

    (root / "audio2").mkdir(parents=True, exist_ok=True)
    (root / "log" / "runs").mkdir(parents=True, exist_ok=True)


def validate(conn: sqlite3.Connection) -> dict[str, object]:
    pcols = columns(conn, "programs")
    rcols = columns(conn, "recordings")
    radio = conn.execute(
        """
        SELECT header_cut_default_seconds,header_cut_fri_seconds
        FROM programs WHERE program_name='ラジオ英会話'
        """
    ).fetchone()
    return {
        "program_count": conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0],
        "status_count": conn.execute("SELECT COUNT(*) FROM recording_status_master").fetchone()[0],
        "missing_program_columns": sorted(set(PROGRAM_COLUMNS)-pcols),
        "missing_recording_columns": sorted(set(RECORDING_COLUMNS)-rcols),
        "radio_english": tuple(radio) if radio else None,
        "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_errors": conn.execute("PRAGMA foreign_key_check").fetchall(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database")
    args = parser.parse_args()

    package_dir = Path(__file__).resolve().parent
    root, db_path = find_root(package_dir, args.database)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = db_path.parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"radio_catalog_before_header_cut_{stamp}.db"
    log_path = backup_dir / f"header_cut_update_{stamp}.log"
    shutil.copy2(db_path, backup_path)

    log = [
        f"STARTED_AT={datetime.now().isoformat(timespec='seconds')}",
        f"ROOT={root}",
        f"DATABASE={db_path}",
        f"BACKUP={backup_path}",
    ]

    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        for required in ("programs", "episodes", "recordings"):
            if not table_exists(conn, required):
                raise RuntimeError(f"{required} table がありません")

        ensure_status_master(conn, log)
        add_missing_columns(conn, "programs", PROGRAM_COLUMNS, log)
        add_missing_columns(conn, "recordings", RECORDING_COLUMNS, log)
        migrate_values(conn, log)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_recording_status_code ON recordings(status_code)")
        recreate_triggers(conn, log)
        recreate_views(conn, log)
        ensure_meta(conn, log)
        conn.commit()

        install_files(package_dir, root, log)

        conn.execute("PRAGMA foreign_keys=ON")
        result = validate(conn)
        log.append(f"VALIDATION={result!r}")
        if result["program_count"] != 5:
            raise RuntimeError(f"programs件数が5ではありません: {result['program_count']}")
        if result["status_count"] != 7:
            raise RuntimeError("ステータスマスターが7件ではありません")
        if result["missing_program_columns"] or result["missing_recording_columns"]:
            raise RuntimeError("必要な列が不足しています")
        if result["radio_english"] != (53, 53):
            raise RuntimeError("ラジオ英会話の53秒設定が不正です")
        if result["integrity"] != "ok":
            raise RuntimeError(f"integrity_check={result['integrity']}")
        if result["foreign_key_errors"]:
            raise RuntimeError(f"foreign_key_check={result['foreign_key_errors']}")

        log.append("RESULT=PASS")
        log_path.write_text("\n".join(log)+"\n", encoding="utf-8")
        print("RESULT=PASS")
        print(f"DATABASE={db_path}")
        print(f"BACKUP={backup_path}")
        print(f"LOG={log_path}")
        print("PROGRAM_COUNT=5")
        print("STATUS_MASTER_COUNT=7")
        print("RADIO_ENGLISH_DEFAULT=53")
        print("RADIO_ENGLISH_FRIDAY=53")
        print("NEXT=RUN_ALL.bat")
        return 0
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        log.append("RESULT=FAIL")
        log.append(f"ERROR={type(exc).__name__}: {exc}")
        log_path.write_text("\n".join(log)+"\n", encoding="utf-8")
        print("RESULT=FAIL")
        print(f"ERROR={type(exc).__name__}: {exc}")
        print(f"BACKUP={backup_path}")
        print(f"LOG={log_path}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
