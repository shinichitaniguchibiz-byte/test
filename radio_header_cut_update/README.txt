Radio SQLite header-cut update
==============================

This package updates the existing radio_sqlite_batch_v1 program.
It does not use Supabase and does not require DBeaver SQL execution.

Files changed or added
----------------------
- database/radio_catalog.db: updated in place after an automatic backup
- RUN_ALL.bat: replaced with download + conversion wrapper
- RUN_DOWNLOAD_ONLY.bat: original RUN_ALL.bat without pause
- RUN_ALL.before_header_cut.bat: untouched backup of the original launcher
- header_cut_processor.py
- CONVERT_PENDING.bat
- RETRY_CONVERT_FAILED.bat
- VERIFY_HEADER_CUT.bat
- audio2/<program_id>/<same file name>

Database additions
------------------
programs:
- header_cut_default_seconds
- header_cut_mon_seconds
- header_cut_tue_seconds
- header_cut_wed_seconds
- header_cut_thu_seconds
- header_cut_fri_seconds

recordings:
- status_code
- original_file_path
- converted_file_path
- applied_cut_seconds
- converted_file_size_bytes
- converted_duration_seconds
- converted_sha256
- conversion_started_at
- conversion_completed_at
- status_description

recording_status_master:
- 0  completed
- 10 pending_download
- 20 downloading
- 30 download_failed
- 40 pending_conversion
- 50 converting
- 60 conversion_failed

Initial setting
---------------
ラジオ英会話:
- default: 53 seconds
- Friday: 53 seconds
- Monday to Thursday: NULL, therefore default 53 seconds is used

Installation
------------
1. Stop DBeaver, RUN_ALL.bat, Python and FFmpeg.
2. Put the complete radio_header_cut_update folder directly under the existing
   radio_sqlite_batch_v1 folder.

   Example:
   radio_sqlite_batch_v1
   |- database
   |- radio_batch.py
   |- RUN_ALL.bat
   `- radio_header_cut_update
      |- APPLY_UPDATE.bat
      |- apply_update.py
      `- ...

3. Double-click:
   radio_header_cut_update\APPLY_UPDATE.bat
4. Confirm RESULT=PASS.
5. Double-click the new RUN_ALL.bat in radio_sqlite_batch_v1.

Normal execution
----------------
RUN_ALL.bat performs:
1. Existing metadata load and download process
2. Save original file as before
3. Set status 40 after download success
4. Choose weekday-specific cut seconds, otherwise default seconds
5. Set status 50 while converting
6. Save the converted file to audio2/<program_id>/ with the same name
7. Set status 0 only after complete success
8. Set status 60 when conversion fails

Conversion failure
------------------
When status_code is 60:
- The original file is not downloaded again.
- The reason is stored in recordings.status_description and last_error.
- Fix the cause or change the cut seconds in programs.
- Run RETRY_CONVERT_FAILED.bat.

Other commands
--------------
CONVERT_PENDING.bat
- Converts downloaded files with status 40.

RETRY_CONVERT_FAILED.bat
- Retries only status 60.
- Does not run the downloader.

VERIFY_HEADER_CUT.bat
- Confirms 5 programs, 7 status rows, required columns, 53-second settings,
  database integrity and installed files.

Backups and logs
----------------
database\backup\radio_catalog_before_header_cut_YYYYMMDD_HHMMSS.db
database\backup\header_cut_update_YYYYMMDD_HHMMSS.log
log\runs\header_cut_pending_YYYYMMDD_HHMMSS.log
log\runs\header_cut_retry-failed_YYYYMMDD_HHMMSS.log
