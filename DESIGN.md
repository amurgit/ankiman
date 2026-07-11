# ankiman — Design

Bulk-fill Anki note fields using an LLM (OpenAI-compatible API) via AnkiConnect.

## What it does

You provide a prompt template with `{FieldName}` placeholders, and `ankiman` processes every note in a deck through the LLM, writing results back to target fields.

## Config files

**`.ankiman_config.yaml`** — named profiles (API model string, base URL, key env var)

```yaml
default: deepseek

profiles:
  deepseek:
    model: deepseek-chat
    api_base: https://api.deepseek.com/v1
    api_key_env: ANKI_LLM_API_KEY
```

**`.env`** — API keys loaded via `python-dotenv` (prompted interactively if missing)

```
ANKI_LLM_API_KEY=sk-...
```

## CLI structure

```
ankiman [-v] {ping, deck list, profile add, fill} ...
```

| Command       | Purpose                                                 |
| ------------- | ------------------------------------------------------- |
| `ping`        | Test AnkiConnect connection (+ optional `--check-key`)  |
| `deck list`   | Numbered deck list for `fill -d N`                      |
| `profile add` | Register a new profile in `.ankiman_config.yaml`        |
| `fill`        | Bulk process notes through LLM                          |

## Example flow

```bash
# 1. Add a profile
ankiman profile add deepseek \
  -n deepseek-chat \
  --api-base https://api.deepseek.com/v1 \
  --set-default

# 2. See available decks
ankiman deck list

# Output:
#   #  Deck                                 Notes
#   1  Default                              0
#   2  Cantonese                            5002

# 3. Fill target fields (uses default profile)
ankiman fill \
  -d 2 \
  -p "Translate {Traditional} to Mandarin. Return JSON: {\"Mandarin_Word\": \"...\", \"Mandarin_Sentence\": \"...\"}" \
  -t "Mandarin_Word,Mandarin_Sentence"

# 4. Specify a config explicitly
ankiman fill -d 2 -p "..." -t "Mandarin_Word" -c deepseek

# 5. Dry run (LLM calls but no Anki writes)
ankiman fill -d 2 -p "..." -t "Mandarin_Word" -n

# 6. Force re-fill already-filled notes
ankiman fill -d 2 -p "..." -t "Mandarin_Word" -f
```

## `fill` flags

| Long                     | Short | Default             | Purpose                                                                 |
| ------------------------ | ----- | ------------------- | ----------------------------------------------------------------------- |
| `--deck`                 | `-d`  | required            | Deck index (from `deck list`) or exact name                             |
| `--prompt`               | `-p`  | required            | Template with `{Field}` placeholders — sources auto-extracted via regex |
| `--target-fields`        | `-t`  | required            | Comma-separated Anki fields to write LLM output into                    |
| `--config`               | `-c`  | YAML `default:`     | Profile name from `.ankiman_config.yaml`                                |
| `--force`                | `-f`  | false               | Re-process even if all target fields already have content               |
| `--allow-partial-source` |       | false               | Process when some (not all) source fields are filled                    |
| `--dry-run`              | `-n`  | false               | Call LLM but do not write to Anki                                       |
| `--delay`                |       | 0.3                 | Seconds between API calls                                               |
| `--model-name`           |       |                     | One-off override of API model string for this run                       |
| `--api-base`             |       |                     | One-off override of API base URL for this run                           |

## `profile add` flags

| Argument        | Short      | Default            | Purpose                               |
| --------------- | ---------- | ------------------ | ------------------------------------- |
| `name`          | positional | required           | Profile name (used with `fill -c`)    |
| `--profile-name` | `-n`      | required           | API model string (e.g. `deepseek-chat`) |
| `--api-base`    |            | required           | OpenAI-compatible base URL            |
| `--api-key-env` |            | `ANKI_LLM_API_KEY` | Environment variable for the API key  |
| `--set-default` |            | false              | Set as default profile in config      |
| `--force`       |            | false              | Overwrite existing profile name       |

## `ping` flags

| Flag          | Purpose                                       |
| ------------- | --------------------------------------------- |
| `--check-key` | Verify API key is set for the selected profile |
| `--config`    | Profile name (optional, uses default)          |

## Processing logic per note

1. **Skip check (source)**: If any source field `{Field}` is empty → skip (unless `--allow-partial-source`)
2. **Skip check (target)**: If all target fields already filled → skip (unless `-f`)
3. **Replace**: `{Traditional}` → actual field value from Anki
4. **LLM call**: Send compiled prompt, retry up to 3× with exponential backoff
5. **Parse**: `json.loads()` the response — strict, must contain ALL target keys (error if missing any)
6. **Write**: `updateNoteFields` with new values (skip if `-n`)
7. **Log**: `[N/TOTAL] note=... source preview -> target preview`

## Skip rules detail

### Source fields

| Condition               | Default | With `--allow-partial-source` |
| ----------------------- | ------- | ----------------------------- |
| All source fields empty | Skip    | Skip                          |
| Some empty, some filled | Skip    | Process (empty → `""`)        |
| All filled              | Process | Process                       |

### Target fields (resume / checkpoint)

| Condition                   | Default       | With `--force`      |
| --------------------------- | ------------- | ------------------- |
| All target fields non-empty | Skip (resume) | Process (overwrite) |
| Any target field empty      | Process       | Process             |

No separate checkpoint file — resume is field-based.

## Logging output example

```
INFO    Deck 'Cantonese': 5002 notes, sources=['Traditional'], targets=['Mandarin_Word', 'Mandarin_Sentence'], profile=deepseek
INFO    [12/5002] note=1776774735423 Traditional='你好' -> Mandarin_Word='你好', Mandarin_Sentence='你好，世界'
WARNING Skipping note 1776774735424: source field 'Traditional' is empty
WARNING Skipping note 1776774735425: all target fields already filled
ERROR   Note 1776774735426: JSON missing required key(s): Mandarin_Sentence
INFO    Done. processed=4500 skipped=480 errors=22 total=5002
```

`-v` (verbose) shows all `DEBUG` logs: full prompts sent, raw LLM responses, AnkiConnect payloads, skip decisions.

## Dependencies

```toml
# pyproject.toml
[project]
dependencies = [
    "requests>=2.31.0",
    "openai>=1.0.0",
    "loguru>=0.7.0",
    "python-dotenv>=1.0.0",
    "pyyaml>=6.0",
]
```

## File layout

```
ankiman/
├── pyproject.toml
├── ankiman/
│   └── __init__.py          # all logic + CLI
├── .ankiman_config.yaml     # created by `profile add`
├── .env                     # API keys (gitignored)
└── DESIGN.md
```
