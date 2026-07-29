NHK downloader V34 complete replacement
=====================================

Files
-----
nhk_download.py
RUN_NHK_DOWNLOAD.bat

Installation
------------
1. Stop the currently running Python and FFmpeg processes.
2. Close Excel once.
3. In the existing radio folder, rename the broken file:
   nhk_download.py -> nhk_download_broken_20260729.txt
4. Copy this package's nhk_download.py and RUN_NHK_DOWNLOAD.bat into:
   C:\OneDrive2\OneDrive\lib\codex\radio
5. Keep radio_recording.xlsx in the same directory.
6. Run:
   python.exe -m py_compile .\nhk_download.py
7. When no error is printed, run:
   python.exe .\nhk_download.py
   or double-click RUN_NHK_DOWNLOAD.bat.

Excel behavior
--------------
- The workbook may already be open or may be closed.
- The program connects to the exact current-directory radio_recording.xlsx.
- AutoSave is diagnostic information only.
- The program never changes AutoSaveOn.
- AutoSave ON/OFF never blocks a download.
- Every verified recording row is explicitly saved with workbook.Save().
- The workbook remains open after the run.

Download behavior
-----------------
- The program worksheet contains the five program definitions.
- recording status=0 is skipped.
- status=1, blank, or missing recording_id is downloaded.
- Downloads use up to 15 concurrent FFmpeg workers.
- Each file is checked by duration, full decode, and SHA-256.
- Excel writes start only after all downloads finish.
- Results are written in planned sequence.
- A successful row receives status=0; an error row receives status=1.

Logs
----
log\YYYYMMDD.log
log\runs\RUN_ID.log
log\runs\RUN_ID_validation.zip
