from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    db = root / "database" / "radio_catalog.db"
    if not db.is_file():
        print(f"RESULT=FAIL\nERROR=Database not found: {db}")
        return 1

    conn = sqlite3.connect(db)
    try:
        program_columns = {row[1] for row in conn.execute("PRAGMA table_info(programs)")}
        recording_columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        required_program = {
            "header_cut_default_seconds",
            "header_cut_mon_seconds",
            "header_cut_tue_seconds",
            "header_cut_wed_seconds",
            "header_cut_thu_seconds",
            "header_cut_fri_seconds",
        }
        required_recording = {
            "status_code",
            "original_file_path",
            "converted_file_path",
            "applied_cut_seconds",
            "converted_file_size_bytes",
            "converted_duration_seconds",
            "converted_sha256",
            "conversion_started_at",
            "conversion_completed_at",
            "status_description",
        }
        program_count = conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        status_count = conn.execute("SELECT COUNT(*) FROM recording_status_master").fetchone()[0]
        radio = conn.execute(
            "SELECT header_cut_default_seconds,header_cut_fri_seconds "
            "FROM programs WHERE program_name='ラジオ英会話'"
        ).fetchone()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        missing_program = sorted(required_program - program_columns)
        missing_recording = sorted(required_recording - recording_columns)
        installed_files = [
            "RUN_ALL.bat",
            "RUN_DOWNLOAD_ONLY.bat",
            "header_cut_processor.py",
            "CONVERT_PENDING.bat",
            "RETRY_CONVERT_FAILED.bat",
        ]
        missing_files = [name for name in installed_files if not (root / name).is_file()]

        print(f"PROGRAM_COUNT={program_count}")
        print(f"STATUS_MASTER_COUNT={status_count}")
        print(f"RADIO_ENGLISH={tuple(radio) if radio else None}")
        print(f"INTEGRITY={integrity}")
        print(f"FOREIGN_KEY_ERRORS={foreign_key_errors}")
        print(f"MISSING_PROGRAM_COLUMNS={missing_program}")
        print(f"MISSING_RECORDING_COLUMNS={missing_recording}")
        print(f"MISSING_FILES={missing_files}")

        passed = (
            program_count == 5
            and status_count == 7
            and tuple(radio or ()) == (53, 53)
            and integrity == "ok"
            and not foreign_key_errors
            and not missing_program
            and not missing_recording
            and not missing_files
        )
        print("RESULT=PASS" if passed else "RESULT=FAIL")
        return 0 if passed else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
