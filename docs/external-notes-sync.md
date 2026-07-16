# External notes: Obsidian, Apple Notes, Google Docs

The ledger never silently reads your whole Google account or Notes library.
Each source is **opt-in** and lands as `note` + `external_note` events (plus an artifact copy).

## Obsidian

1. Set the vault path:

```bash
export ANALYST_OBSIDIAN_VAULT="$HOME/Obsidian/MyVault"
# optional: only a subfolder
export ANALYST_OBSIDIAN_SUBDIR="Research"
```

2. Mark notes for ingest with frontmatter **or** a `#ledger` tag:

```markdown
---
ledger: true
sensitivity: internal
symbol: NVDA
---

Thesis notes…
```

or:

```markdown
Morning scan thoughts #ledger
```

3. Sync:

```bash
analyst sync obsidian --once
# or watch:
analyst sync obsidian
```

`--all-notes` ingests every markdown file (usually too broad for HF use).

## Apple Notes

1. In Notes.app, create a folder named **`AnalystLedger`** (or set `ANALYST_APPLE_NOTES_FOLDER`).
2. Put only research notes you want synced in that folder.
3. On first run, macOS may ask for Automation access to Notes.

```bash
analyst sync apple-notes --once
# export only (no ingest):
analyst sync apple-notes --export-only
# ingest previously exported files without calling Notes:
analyst sync apple-notes --once --skip-export
```

Exports land in `data/apple_notes_export/` (or `ANALYST_APPLE_NOTES_EXPORT`).

## Google Docs

There is **no** Google OAuth in v1. Use a dedicated export / Drive Desktop folder:

```bash
export ANALYST_GDOCS_EXPORT="$HOME/AnalystGDocs"   # default
mkdir -p "$HOME/AnalystGDocs"
```

From Google Docs: **File → Download → Plain text / Markdown / Microsoft Word (.docx)**  
into that folder (or sync the folder with Drive for desktop).

```bash
analyst sync gdocs --once
analyst sync gdocs   # watch
```

Supported: `.md`, `.txt`, `.docx`. Add `ledger: false` in frontmatter to skip a file.

## Sync everything once

```bash
analyst sync all
```

## What the agent “sees”

After sync, ritual mining and `synthesize` see the same `note` text as CLI notes.
`restricted` sensitivity still never egresses to Claude.
