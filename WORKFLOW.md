# Radio Lesson Transcription Workflow

## Step 1: Create the raw transcript

Place `TRANSCRIBE_B.py` in the radio working directory and run:

```powershell
py .\TRANSCRIBE_B.py
```

The raw transcript is saved under:

```text
stt_test\YYYYMMDD_HHMMSS.txt
```

## Step 2: Format with an AI model

Open `TRANSCRIPT_FORMATTING_PROMPT.md`.

Replace `{{RAW_TRANSCRIPT}}` with the complete raw transcript.

When a trusted reference transcript is available, replace `{{REFERENCE_TRANSCRIPT_OR_LEAVE_EMPTY}}` with that reference. Otherwise leave that block empty.

Submit the completed prompt to GPT or another capable AI model.

## Step 3: Save the formatted lesson

Save the returned Markdown as a separate file. Do not overwrite the raw transcript. The raw transcript remains the source record for later comparison and correction.
