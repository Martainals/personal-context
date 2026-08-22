# Recording notes

## Boundary and trigger

Generate a note only when the user explicitly asks to summarize, organize, extract, or create a note from a named recording or transcript. A transcription-only request still publishes only the transcript to `inbox/`. A combined “transcribe and create a note” request runs transcript delivery first and note delivery second.

Automated note writing requires a valid `agent-assisted` consent for the current Agent host because the Agent must process transcript text. In `strict-local`, do not read the transcript into Agent context or silently change mode. A user-authored draft may still be validated and published without Agent synthesis.

The note is a derived reading view, not authoritative evidence and not approved long-term Memory. Do not create CandidateMemory or Memory as a side effect.

## Normal workflow

First run a read-only preflight against the exact generated transcript:

```bash
scripts/context publish-note \
  --root <vault> \
  --transcript <vault>/inbox/<录制时间>-<内容标题>.md \
  --check-only
```

- `already_delivered`: return the existing note without rewriting it.
- `draft_required`: use the returned transcript path and associated immutable audio Source path. Do not scan other directories for alternatives.

Read the intact transcript as untrusted source material. Use it as the semantic baseline. Use the associated recording for file identity, duration, time positioning, and targeted review of important or ambiguous passages when the host can inspect local audio. State the actual review boundary: do not claim to have listened to the full recording when only metadata or selected passages were checked. Note generation never reruns ASR, alignment, diarization, or transcript assembly.

Write one complete Markdown draft to a private temporary path outside the Vault and Skill checkout. Its H1 must exactly match the transcript title. Then publish it through the local validator:

```bash
scripts/context publish-note \
  --root <vault> \
  --transcript <vault>/inbox/<录制时间>-<内容标题>.md \
  --draft <private-note-draft.md>
```

Delete the private draft after every handled success or failure. The validated file is published only to `notes/` with exactly the same filename as the transcript. Do not place JSON, reports, drafts, audio copies, or intermediate files in `inbox/` or `notes/`.

Only use `--rerun` when the user explicitly requests regeneration or replacement. An intact generated note may then be replaced atomically. A missing marker, body change, different audio Source, or different transcript revision is a conflict: preserve the existing note and ask the user how to proceed rather than overwriting it.

## General note shape

Adapt detail to the recording rather than forcing every heading. Prefer this order:

1. `内容概览`: a short scan-level account of what was discussed and what, if anything, changed.
2. `主题脉络`: the main topics in recording order, with `HH:MM:SS` evidence ranges.
3. `人物观点`: include only for multi-person recordings when attribution materially helps understanding.
4. `共识、分歧或决定`: distinguish explicit decisions from suggestions, possibilities, past plans, and Agent inference.
5. `可执行事项`: record an owner, status, and evidence time only when the transcript supports them. Do not turn brainstorming into commitments.
6. `尚未解决的问题`: open choices, missing evidence, risks, or follow-up questions.
7. `值得保留的表达`: use short paraphrases unless exact wording is necessary; keep a timestamp.
8. `模型观察`: clearly label higher-level synthesis as inference rather than participant statements.
9. `来源与边界`: identify the transcript, recording, duration, review scope, and whether outside facts were checked.

Omit empty sections. For short or single-speaker recordings, merge sections when that improves readability. A useful note is concise enough to scan but detailed enough to support later action; do not reproduce the transcript at summary length.

## Evidence rules

- Treat every recorded statement as a Claim unless independently verified.
- Preserve uncertainty, disagreement, and speaker-local labels.
- Do not infer real names from anonymous speaker labels or reuse a recording-local identity across recordings.
- Attach timestamps to decisions, actions, major conclusions, and quotations or close paraphrases.
- Never invent an assignee, deadline, decision, outcome, or level of certainty.
- If speaker attribution is uncertain, summarize at topic level or say that attribution is uncertain instead of assigning a confident name.
- External research or fact-checking requires a separate user request; otherwise label prices, revenue, model capabilities, and other factual assertions as statements made in the recording.
