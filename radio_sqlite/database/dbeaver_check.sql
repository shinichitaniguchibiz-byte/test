-- Run each statement in DBeaver after opening database\radio.db.

PRAGMA integrity_check;
PRAGMA foreign_key_check;

SELECT
    program_id,
    program_name,
    program_abbreviation,
    is_active,
    site_id,
    corner_id,
    output_directory,
    directory_name
FROM program
ORDER BY program_id;

SELECT
    status,
    COUNT(*) AS recording_count
FROM recording
GROUP BY status
ORDER BY status;

SELECT
    recording_id,
    program_name,
    onair_at,
    episode_title_sub,
    status,
    file_name,
    relative_path,
    updated_at
FROM recording
ORDER BY COALESCE(onair_at, '') DESC, recording_id DESC
LIMIT 100;

SELECT
    run_id,
    command_name,
    started_at,
    ended_at,
    status,
    planned_count,
    success_count,
    skip_count,
    error_count,
    exit_code,
    log_path,
    validation_zip_path
FROM run
ORDER BY started_at DESC
LIMIT 50;
