# ankiman — Design

Bulk-fill Anki note fields using an LLM (OpenAI-compatible API) via AnkiConnect.

## What it does

You provide a prompt template with `{FieldName}` placeholders, and `ankiman` processes every note in a deck through the LLM, writing results back to target fields.

## File layout

```
ankiman/
├── pyproject.toml
├── ankiman/
│   ├── __init__.py          # re-exports main
│   ├── cli.py               # Typer CLI, orchestration
│   ├── anki.py              # AnkiConnect client, field helpers
│   ├── llm.py               # LLM client, retries, DNS cache
│   └── config.py            # config/profile read/write, env
├── .ankiman_config.yaml     # created by `model add`
├── .env                     # API keys (gitignored)
├── README.md
└── DESIGN.md
```

## Config files

**`.ankiman_config.yaml`** — named models (API model string, base URL, key env var)

```yaml
default: deepseek

models:
  deepseek:
    model: deepseek-chat
    api_base: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
```

**`.env`** — API keys loaded via `python-dotenv` (prompted interactively if missing)

```
DEEPSEEK_API_KEY=sk-...
```

The env var defaults to `{NAME}_API_KEY` derived from the model name (e.g. `deepseek` → `DEEPSEEK_API_KEY`). Override with `--api-key-env` when adding.

## CLI structure

```
ankiman [-v|-vv] {ping, deck list, deck fields, model add|list|default|balance, fill} ...
```

| Command         | Purpose                                                 |
| --------------- | ------------------------------------------------------- |
| `ping`          | Test AnkiConnect connection (+ optional `--check-key`)  |
| `deck list`     | Numbered deck list for `fill -d N`                      |
| `deck fields`   | Show field names for a deck with example values         |
| `model add`     | Register a new model in `.ankiman_config.yaml`          |
| `model list`    | List all configured models                              |
| `model default` | Switch the active (default) model                       |
| `model balance` | Check API account balance for a model                   |
| `fill`          | Bulk process notes through LLM                          |

## Example flow

```bash
# 1. Add a model
ankiman model add deepseek \
  -m deepseek-chat \
  --api-base https://api.deepseek.com/v1 \
  --set-default

# 2. See available decks
ankiman deck list

# 3. Inspect field names for a deck (shows real note values)
ankiman deck fields -d 2

# 4. Dry-run to test the prompt (LLM calls but no Anki writes)
ankiman fill \
  -d 2 \
  -p "Give the Mandarin analogue for the Cantonese word {Cantonese}" \
  -t MandarinAnalogue \
  -n

# 5. Fill for real (uses default model)
ankiman fill -d 2 -p "Give Mandarin analogue for {Cantonese}" -t MandarinAnalogue

# 6. Limit to 10 notes for testing
ankiman fill -d 2 -p "..." -t MandarinAnalogue -l 10

# 7. Parallel mode — 5 LLM calls at once
ankiman fill -d 2 -p "..." -t MandarinAnalogue -b 5

# 8. Override model for one run
ankiman fill -d 2 -p "..." -t MandarinAnalogue -c openai

# 9. Force re-fill already-filled notes
ankiman fill -d 2 -p "..." -t MandarinAnalogue -f

# 10. Raw prompt — provide your own JSON format
ankiman fill -d 2 -p "Give Mandarin analogue for {Cantonese}. Return JSON: {\"MandarinAnalogue\": \"...\", \"MandarinSentence\": \"...\"}" -t MandarinAnalogue,MandarinSentence --raw-prompt
```

## `fill` flags

| Long                     | Short | Default          | Purpose                                                                 |
| ------------------------ | ----- | ---------------- | ----------------------------------------------------------------------- |
| `--deck`                 | `-d`  | required         | Deck index (from `deck list`) or exact name                             |
| `--prompt`               | `-p`  | required         | Template with `{Field}` placeholders — sources auto-extracted via regex |
| `--target-fields`        | `-t`  | required         | Comma-separated Anki fields to write LLM output into                    |
| `--config`               | `-c`  | YAML `default:`  | Model name from `.ankiman_config.yaml`                                  |
| `--force`                | `-f`  | false            | Re-process even if all target fields already have content               |
| `--allow-partial-source` |       | false            | Process when some (not all) source fields are filled                    |
| `--dry-run`              | `-n`  | false            | Call LLM but do not write to Anki                                       |
| `--wait`                 | `-w`  | 0                | Seconds between batches                                                 |
| `--batch`                | `-b`  | 1                | Parallel LLM calls per batch (1 = sequential)                           |
| `--limit-count`          | `-l`  | 0                | Process at most N notes (0 = no limit)                                  |
| `--raw-prompt`           |       | false            | Disable auto-generated JSON format instruction                          |
| `--model-name`           |       |                  | One-off override of API model string for this run                       |
| `--api-base`             |       |                  | One-off override of API base URL for this run                           |

## `model add` flags

| Argument       | Short  | Default | Purpose                               |
| -------------- | ------ | ------- | ------------------------------------- |
| `name`         | pos    | required| Model name (used with `fill -c`)      |
| `--model`      | `-m`   | required| API model string (e.g. `deepseek-chat`)|
| `--api-base`   |        | required| OpenAI-compatible base URL            |
| `--api-key-env`|        | derived | Env var for API key (default: `{NAME}_API_KEY`) |
| `--set-default`|        | false   | Set as default model in config        |
| `--force`      |        | false   | Overwrite existing model name         |

## `ping` flags

| Flag          | Purpose                                       |
| ------------- | --------------------------------------------- |
| `--check-key` | Verify API key is set for the selected model  |
| `--config`    | Model name (optional, uses default)           |

## Processing logic

### Pre-scan

Before any LLM calls, all notes are scanned to count:
- **skipped (source)**: notes with empty `{Field}` placeholders
- **skipped (target)**: notes with all target fields already filled
- **eligible**: notes that will actually be processed

Stats are logged: `Pre-scan: N eligible, M skipped (source empty), K skipped (target filled)`.

### Per note (sequential or parallel batch)

1. **Skip check (source)**: If any source field is empty → skip (unless `--allow-partial-source`)
2. **Skip check (target)**: If all target fields already filled → skip (unless `-f`)
3. **Replace**: `{Cantonese}` → actual field value from Anki. JSON format is auto-appended from `--target-fields` (unless `--raw-prompt`).
4. **LLM call**: Send compiled prompt, retry up to 3× with randomized exponential backoff (2s base, 1-3s → 3-7s)
5. **Parse**: `json.loads()` with fence-stripping — must contain ALL target keys (error if missing any)
6. **Progress log**: one line per note — `Cantonese=嘅, SentenceCantonese=我係佢嘅朋友。 → MandarinAnalogue=的`
7. **Write**: `updateNoteFields` with new values (skip if `-n`)

Progress counter shows `[processed/eligible]`, not `[N/total]`.

### Batch mode (`-b N`)

When `--batch` > 1, N prompt+LLM requests run in parallel via `ThreadPoolExecutor`. Anki updates and logging remain sequential. Ctrl+C cancels pending futures and exits cleanly via `os._exit`.

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

## Verbosity levels

| Flag   | Level   | Shows                                      |
| ------ | ------- | ------------------------------------------ |
| (none) | INFO    | Progress lines, errors, summary            |
| `-v`   | DETAIL  | + prompts sent and raw LLM responses       |
| `-vv`  | DEBUG   | + AnkiConnect payloads, retry timing       |

## Error handling

- **DNS**: `socket.getaddrinfo` is monkey-patched with a per-run cache — hostnames resolve once.
- **FD leaks**: `resp.close()` is called explicitly after every HTTP request. `RLIMIT_NOFILE` is raised to 4096 on startup.
- **EMFILE diagnostics**: when `[Errno 24] Too many open files` is detected, current open FD count and limits are logged.
- **Ctrl+C**: `SIGINT` handler calls `os._exit(1)` immediately — no threading traceback.
- **AnkiConnect errors**: caught and shown as user-facing messages (no stacktraces).

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
