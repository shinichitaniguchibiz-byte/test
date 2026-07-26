from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "radio_header_cut_update"


def create_old_database(path: Path, root: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE programs(
                program_id INTEGER PRIMARY KEY AUTOINCREMENT,
                program_name TEXT NOT NULL UNIQUE,
                program_url TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                output_directory TEXT NOT NULL DEFAULT '',
                directory_name TEXT NOT NULL DEFAULT '',
                program_abbreviation TEXT NOT NULL DEFAULT '',
                site_id TEXT NOT NULL DEFAULT '',
                corner_id TEXT NOT NULL DEFAULT '01',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE episodes(
                recording_id TEXT PRIMARY KEY,
                program_id INTEGER NOT NULL,
                onair_at TEXT,
                display_title TEXT NOT NULL DEFAULT '',
                episode_title TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                player_url TEXT NOT NULL DEFAULT '',
                closed_at TEXT,
                availability_status TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(program_id) REFERENCES programs(program_id)
            );
            CREATE TABLE recordings(
                recording_id TEXT PRIMARY KEY,
                download_status TEXT NOT NULL DEFAULT 'not_downloaded',
                file_name TEXT NOT NULL DEFAULT '',
                saved_directory TEXT NOT NULL DEFAULT '',
                file_size_bytes INTEGER NOT NULL DEFAULT 0,
                expected_duration_seconds REAL,
                actual_duration_seconds REAL,
                segment_count INTEGER,
                sha256 TEXT NOT NULL DEFAULT '',
                downloaded_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_run_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(recording_id) REFERENCES episodes(recording_id)
            );
            CREATE TABLE app_meta(
                meta_key TEXT PRIMARY KEY,
                meta_value TEXT NOT NULL
            );
            """
        )
        programs = [
            (1, "ラジオ英会話", "RBE"),
            (2, "テスト番組", "TST"),
            (3, "番組3", "P03"),
            (4, "番組4", "P04"),
            (5, "番組5", "P05"),
        ]
        for program_id, name, abbreviation in programs:
            conn.execute(
                """
                INSERT INTO programs(
                    program_id,program_name,program_url,is_active,
                    output_directory,directory_name,program_abbreviation,
                    site_id,corner_id
                ) VALUES(?,?,?,1,?,?,?,'site','01')
                """,
                (
                    program_id,
                    name,
                    f"https://example.invalid/{program_id}",
                    str(root / "audio"),
                    str(program_id),
                    abbreviation,
                ),
            )
        original_dir = root / "audio" / "2"
        original_dir.mkdir(parents=True, exist_ok=True)
        wav_path = original_dir / "test.wav"
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 8000)
        conn.execute(
            """
            INSERT INTO episodes(
                recording_id,program_id,onair_at,display_title,episode_title,
                description,player_url,availability_status
            ) VALUES('rec-1',2,'2026-07-20 08:00:00','','Test episode','','','available')
            """
        )
        conn.execute(
            """
            INSERT INTO recordings(
                recording_id,download_status,file_name,saved_directory,
                file_size_bytes,actual_duration_seconds,downloaded_at
            ) VALUES('rec-1','downloaded','test.wav',?,?,1.0,datetime('now'))
            """,
            (str(original_dir), wav_path.stat().st_size),
        )
        conn.commit()
    finally:
        conn.close()


def run_test() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "radio_sqlite_batch_v1"
        db_dir = root / "database"
        db_dir.mkdir(parents=True)
        db = db_dir / "radio_catalog.db"
        create_old_database(db, root)
        (root / "RUN_ALL.bat").write_text(
            "@echo off\r\necho original download\r\npause\r\nexit /b 0\r\n",
            encoding="utf-8-sig",
        )

        command = [
            sys.executable,
            str(PACKAGE / "apply_update.py"),
            "--database",
            str(db),
        ]
        first = subprocess.run(command, capture_output=True, text=True)
        assert first.returncode == 0, first.stdout + first.stderr
        second = subprocess.run(command, capture_output=True, text=True)
        assert second.returncode == 0, second.stdout + second.stderr

        conn = sqlite3.connect(db)
        try:
            pcols = {row[1] for row in conn.execute("PRAGMA table_info(programs)")}
            rcols = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
            assert "header_cut_default_seconds" in pcols
            assert "header_cut_fri_seconds" in pcols
            assert "status_code" in rcols
            assert "converted_file_path" in rcols
            assert conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0] == 5
            assert conn.execute("SELECT COUNT(*) FROM recording_status_master").fetchone()[0] == 7
            assert conn.execute(
                "SELECT header_cut_default_seconds,header_cut_fri_seconds "
                "FROM programs WHERE program_name='ラジオ英会話'"
            ).fetchone() == (53, 53)
            assert conn.execute(
                "SELECT status_code FROM recordings WHERE recording_id='rec-1'"
            ).fetchone()[0] == 40
        finally:
            conn.close()

        processor = subprocess.run(
            [
                sys.executable,
                str(root / "header_cut_processor.py"),
                "--mode",
                "pending",
                "--database",
                str(db),
            ],
            capture_output=True,
            text=True,
        )
        assert processor.returncode == 0, processor.stdout + processor.stderr
        output = root / "audio2" / "2" / "test.wav"
        assert output.is_file() and output.stat().st_size > 0

        conn = sqlite3.connect(db)
        try:
            row = conn.execute(
                """
                SELECT status_code,applied_cut_seconds,converted_file_path,
                       converted_file_size_bytes,status_description
                FROM recordings WHERE recording_id='rec-1'
                """
            ).fetchone()
            assert row[0] == 0
            assert row[1] == 0
            assert Path(row[2]) == output
            assert row[3] > 0
            assert "正常に終了" in row[4]
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()

        required = [
            "RUN_ALL.bat",
            "RUN_DOWNLOAD_ONLY.bat",
            "RUN_ALL.before_header_cut.bat",
            "header_cut_processor.py",
            "CONVERT_PENDING.bat",
            "RETRY_CONVERT_FAILED.bat",
            "VERIFY_HEADER_CUT.bat",
        ]
        for name in required:
            assert (root / name).is_file(), name


if __name__ == "__main__":
    run_test()
    print("RESULT=PASS")
