RADIO SQLITE HEADLESS SYSTEM
Version: 20260726_sqlite_headless_v1

1. PURPOSE

This system has no application screen and does not use Radio Manager or Excel as a database.
A normal Windows batch file starts one Python program. DBeaver is used only to inspect or edit the local SQLite database.
No installer is used.

2. DATABASE FILE

The system uses one file:

    database\radio.db

One SQLite file contains all tables. A separate database file is not created for each table.

Main tables:

    program             Program master data
    recording           NHK episode and downloaded-file data
    run                 One row for each Python batch run
    download_attempt    One row for each FFmpeg attempt
    schema_version      Database version

Views for DBeaver:

    v_program
    v_recording
    v_run

3. DIRECTORY

    radio_sqlite\
    ├─ app\
    │  └─ radio_batch.py
    ├─ batch\
    │  ├─ 00_initialize_database.bat
    │  ├─ 01_load_metadata_all.bat
    │  ├─ 02_download_pending_all.bat
    │  ├─ 03_run_all.bat
    │  ├─ 04_verify_database.bat
    │  ├─ run_program.bat
    │  ├─ migrate_existing_sqlite.bat
    │  ├─ 98_force_unlock.bat
    │  └─ 99_self_test.bat
    ├─ database\
    │  ├─ schema.sql
    │  ├─ seed_programs.sql
    │  ├─ dbeaver_check.sql
    │  └─ radio.db                 Created by 00_initialize_database.bat
    ├─ audio\
    ├─ log\runs\
    ├─ .nhk_download_work\
    └─ RUN_ALL.bat

4. FIRST TEST

Step 1
Run:

    batch\99_self_test.bat

Expected result:

    SELF_TEST=PASS
    recording_count=1
    status=pending
    integrity_check=ok

The test uses a temporary database, inserts one test row into recording, checks it, and deletes the temporary database. It does not access NHK and does not run FFmpeg.

Step 2
Run:

    batch\00_initialize_database.bat

This creates database\radio.db, creates every table, and writes the five current programs to program.
Running this batch again does not delete recording rows.

5. OPEN WITH DBEAVER

1. Start DBeaver.
2. Database -> New Database Connection -> SQLite.
3. Select this file:

       <this directory>\database\radio.db

4. Finish the connection.
5. Open main -> Tables -> program.
6. Confirm that five rows exist.
7. Open database\dbeaver_check.sql in DBeaver when SQL checks are needed.

Before starting a Python batch, save or roll back all edits in DBeaver. Do not leave an uncommitted write transaction open.

6. PROVE THAT NHK DATA ENTERS RECORDING

Keep DBeaver open, then run:

    batch\01_load_metadata_all.bat

This batch performs only the following work:

1. Reads active rows from program in database\radio.db.
2. Requests current episode data from the NHK Radio API.
3. Inserts or updates the episodes in recording.
4. Leaves new rows with status=pending.
5. Writes a run row and a timestamped log.
6. Creates a validation ZIP in log\runs.

It does not download audio.

In DBeaver:

1. Open recording.
2. Press Refresh or F5.
3. Confirm that rows exist.
4. Confirm that recording_id has the form:

       site_id_corner_id_episode_id

5. Confirm that status is pending.
6. Run the statements in database\dbeaver_check.sql.

7. DOWNLOAD AUDIO

Run:

    batch\02_download_pending_all.bat

The batch selects pending and error rows, starts FFmpeg, writes to .nhk_download_work first, validates the file, and then moves the completed file into audio.
The recording row is updated to downloaded only after the completed file exists.

FFmpeg is searched in this order:

1. Environment variable RADIO_FFMPEG
2. radio_sqlite\bin\ffmpeg.exe
3. C:\OneDrive2\OneDrive\lib\bin\ffmpeg.exe
4. Windows PATH

FFprobe is optional but should be placed beside ffmpeg.exe. It is used to check duration.

8. NORMAL DAILY RUN

Run:

    RUN_ALL.bat

or:

    batch\03_run_all.bat

This loads current NHK metadata and downloads pending or previously failed rows.

To run only one program:

    batch\run_program.bat ESE
    batch\run_program.bat RBE
    batch\run_program.bat NME
    batch\run_program.bat REC
    batch\run_program.bat ETT

A program_id, abbreviation, or exact program name can be supplied.

9. MOVE DATA FROM AN EARLIER SQLITE FILE

After initializing the new database, run:

    batch\migrate_existing_sqlite.bat "C:\full\path\to\radio_catalog.db"

The importer searches for program or programs and recording or recordings, maps known column names, and writes the rows into the new program and recording tables.
The source file is read only. It is not changed.

10. LOGS AND WORK FILES

Logs:

    log\runs\YYYYMMDD_HHMMSS_xxxxxxxx.log

Validation package:

    log\runs\YYYYMMDD_HHMMSS_xxxxxxxx_validation.zip

Work directory used during download:

    .nhk_download_work\<run_id>\

Process lock:

    .radio_sqlite.lock

A lock prevents two batches from writing to the same database and audio file at the same time. Use batch\98_force_unlock.bat only after confirming that no Radio Python process is running.

11. DATABASE SAFETY

SQLite settings used by every Python connection:

    foreign_keys=ON
    journal_mode=WAL
    synchronous=NORMAL
    busy_timeout=30000

The database uses foreign keys from recording to program. A program row that has recording rows cannot be deleted accidentally. Set is_active=0 instead.

12. PROGRAM TABLE FIELDS TO EDIT IN DBEAVER

Normally edit only:

    program_name
    program_url
    is_active
    output_directory
    directory_name
    program_abbreviation
    english_title
    site_id
    corner_id

output_directory may be a relative path such as audio or a full Windows path.
A relative path is resolved from the radio_sqlite directory.

13. REQUIREMENTS

Python 3.10 or later.
No third-party Python package is required.
FFmpeg is required only for audio download.
FFprobe is recommended for duration checks.
Internet access is required for live NHK metadata and audio.
