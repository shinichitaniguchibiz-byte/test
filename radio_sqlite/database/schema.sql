PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS schema_version (
    version_no INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS program (
    program_id INTEGER PRIMARY KEY,
    program_name TEXT NOT NULL UNIQUE,
    program_url TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    output_directory TEXT NOT NULL DEFAULT 'audio',
    directory_name TEXT NOT NULL,
    program_abbreviation TEXT NOT NULL UNIQUE,
    english_title TEXT,
    site_id TEXT NOT NULL,
    corner_id TEXT NOT NULL DEFAULT '01',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (site_id, corner_id)
);

CREATE TABLE IF NOT EXISTS recording (
    recording_id TEXT PRIMARY KEY,
    program_id INTEGER NOT NULL,
    program_name TEXT NOT NULL,
    program_abbreviation TEXT NOT NULL,
    english_title TEXT,
    program_url TEXT NOT NULL,
    site_id TEXT NOT NULL,
    corner_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    series_title TEXT,
    episode_title TEXT NOT NULL,
    episode_title_sub TEXT,
    episode_sub_title TEXT,
    onair_at TEXT,
    closed_at TEXT,
    stream_url TEXT NOT NULL,
    player_url TEXT,
    file_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    downloaded_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'downloading', 'downloaded', 'skipped', 'error')),
    description TEXT,
    file_size INTEGER,
    expected_duration_seconds REAL,
    actual_duration_seconds REAL,
    segment_count INTEGER,
    sha256 TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (program_id) REFERENCES program(program_id)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    UNIQUE (site_id, corner_id, episode_id)
);

CREATE TABLE IF NOT EXISTS run (
    run_id TEXT PRIMARY KEY,
    command_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'partial', 'error')),
    program_count INTEGER NOT NULL DEFAULT 0,
    planned_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    skip_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER,
    log_path TEXT,
    validation_zip_path TEXT,
    description TEXT
);

CREATE TABLE IF NOT EXISTS download_attempt (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    recording_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('running', 'success', 'error', 'skipped')),
    ffmpeg_command TEXT,
    exit_code INTEGER,
    error_message TEXT,
    staging_path TEXT,
    final_path TEXT,
    file_size INTEGER,
    actual_duration_seconds REAL,
    sha256 TEXT,
    FOREIGN KEY (run_id) REFERENCES run(run_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (recording_id) REFERENCES recording(recording_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    UNIQUE (run_id, recording_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS idx_program_active
    ON program(is_active, program_id);
CREATE INDEX IF NOT EXISTS idx_recording_program_onair
    ON recording(program_id, onair_at DESC);
CREATE INDEX IF NOT EXISTS idx_recording_status
    ON recording(status, onair_at);
CREATE INDEX IF NOT EXISTS idx_download_attempt_recording
    ON download_attempt(recording_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_started
    ON run(started_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_program_updated_at
AFTER UPDATE ON program
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE program
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
     WHERE program_id = NEW.program_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_recording_updated_at
AFTER UPDATE ON recording
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE recording
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%f+09:00', 'now', '+9 hours')
     WHERE recording_id = NEW.recording_id;
END;

CREATE VIEW IF NOT EXISTS v_program AS
SELECT
    p.program_id,
    p.program_name,
    p.program_abbreviation,
    p.is_active,
    p.site_id,
    p.corner_id,
    p.output_directory,
    p.directory_name,
    COUNT(r.recording_id) AS recording_count,
    SUM(CASE WHEN r.status = 'downloaded' THEN 1 ELSE 0 END) AS downloaded_count,
    SUM(CASE WHEN r.status = 'error' THEN 1 ELSE 0 END) AS error_count,
    MAX(r.onair_at) AS latest_onair_at,
    p.updated_at
FROM program p
LEFT JOIN recording r ON r.program_id = p.program_id
GROUP BY p.program_id;

CREATE VIEW IF NOT EXISTS v_recording AS
SELECT
    r.recording_id,
    r.program_id,
    r.program_name,
    r.program_abbreviation,
    r.onair_at,
    r.episode_title,
    r.episode_title_sub,
    r.status,
    r.file_name,
    r.relative_path,
    r.file_size,
    r.expected_duration_seconds,
    r.actual_duration_seconds,
    r.downloaded_at,
    r.last_error,
    r.updated_at
FROM recording r;

CREATE VIEW IF NOT EXISTS v_run AS
SELECT
    run_id,
    command_name,
    started_at,
    ended_at,
    status,
    program_count,
    planned_count,
    success_count,
    skip_count,
    error_count,
    exit_code,
    log_path,
    validation_zip_path
FROM run;
