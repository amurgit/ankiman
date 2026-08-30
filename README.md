# ankiman

Bulk-fill Anki note fields with an LLM through AnkiConnect.

## Requirements

- Python `>= 3.13`
- `uv`
- Anki desktop
- AnkiConnect: https://ankiweb.net/shared/info/2055492159
- An OpenAI-compatible API

## Install

```bash
uv sync
```

## Quick Start

1. Install AnkiConnect in Anki:

```text
Tools -> Add-ons -> Get Add-ons...
Add-on code: 2055492159
```

After installing, restart Anki. AnkiConnect runs at `http://localhost:8765`.

2. Start Anki with AnkiConnect enabled.
3. Add an LLM model. Run interactively (prompts for name, model, API base, and key):

```bash
uv run ankiman model add
```

Or pass everything as flags:

```bash
uv run ankiman model add deepseek \
  --model deepseek-chat \
  --api-base https://api.deepseek.com/v1 \
  --api-key sk-... \
  --set-default
```

4. Check connection:

```bash
uv run ankiman ping
```

5. List decks:

```bash
uv run ankiman deck list
```

6. Show fields for the deck, with real example values from a note:

```bash
uv run ankiman deck fields -d 2
```

7. Fill fields. Example: generate Mandarin analogues for Cantonese words. Deck `_Cantonese HSK 1-5` has fields `Cantonese`, `MandarinAnalogue`.

```bash
uv run ankiman fill \
  -d _Cantonese\ HSK\ 1-5 \
  -p 'Give the Mandarin analogue for the Cantonese word {Cantonese}' \
  -t MandarinAnalogue
```

`{Cantonese}` is replaced with real note values. The JSON format `{"MandarinAnalogue": "..."}` is auto-generated from `--target-fields`. The LLM response is parsed and written back to Anki. Use `--raw-prompt` to provide your own format instructions.

If the API key is missing at runtime, `ankiman` prompts for it. On macOS keys are stored in Keychain; on other platforms they are saved to `.env`.

## Config

`.ankiman_config.yaml`

```yaml
# .ankiman_config.yaml
default: deepseek
models:
  deepseek:
    model: deepseek-chat
    api_base: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
```

`.env` (non-macOS only, or legacy keys)

```dotenv
DEEPSEEK_API_KEY=sk-...
```

On macOS, keys live in Keychain under service `ankiman` instead.

## Main Commands

```bash
uv run ankiman ping
uv run ankiman deck list
uv run ankiman deck fields -d DECK
uv run ankiman model add [NAME] [--model API_MODEL] [--api-base URL] [--api-key KEY] [--set-default]
uv run ankiman model list
uv run ankiman model default NAME
uv run ankiman model balance [-c MODEL]
uv run ankiman fill -d DECK -p PROMPT -t FIELD1,FIELD2
uv run ankiman show [-d DECK] [-g TAGS] [-l N]
```

Useful `fill` flags:

- `-c`, `--config`: choose model
- `-f`, `--force`: overwrite filled target fields
- `-n`, `--dry-run`: do not write to Anki
- `--allow-partial-source`: allow some source fields to be empty
- `-w`, `--wait`: seconds between batches (default 0)
- `-b`, `--batch`: parallel LLM calls per batch (default 1 = sequential)
- `-l`, `--limit-count`: process at most N notes (0 = all)
- `-r`, `--raw-prompt`: disable auto-generated JSON format (provide your own)
- `-g`, `--tags`: filter by tags (comma-separated, OR logic)
- `-v`, `-vv`: debug logs (`-v`/`-vv` show prompts, LLM responses, AnkiConnect payloads)

## Prompt Format

Find field names first with `uv run ankiman deck fields -d DECK`.

Use Anki fields as placeholders:

```text
Give the Mandarin analogue for the Cantonese word {Cantonese}.
```

The JSON format is auto-generated from `--target-fields`. For example, `-t MandarinAnalogue` appends:

```text
Return JSON: {"MandarinAnalogue": "..."}
```

Use `--raw-prompt` to provide your own complete format instructions.

## Show Command

Browse notes with filtered queries:

```bash
uv run ankiman show -d 2 -l 5             # first 5 notes in deck 2
uv run ankiman show -g "to_review" -l 10  # 10 notes with tag
uv run ankiman show -d 3 -g "urgent"      # deck 3 + tag filter
uv run ankiman show -d 2 -f               # show full field values (no truncation)
```

Show flags:

- `-d`, `--deck`: filter by deck
- `-g`, `--tags`: filter by tags (comma-separated, OR)
- `-l`, `--limit-count`: show at most N notes
- `-f`, `--full`: show full field content (not truncated)

At least `--deck` or `--tags` is required.

## Notes

- Deck can be a number from `deck list` or an exact name.
- Notes with empty source fields are skipped by default.
- Notes with all target fields already filled are skipped unless `--force` is used.
- Target/source fields must exist on the note type.
- Progress shows `[processed/eligible]` — the denominator is only notes that will actually be processed.
- Ctrl+C exits cleanly, no traceback.
- Connection errors retry up to 3× with randomized backoff (1-3s → 3-7s).

### Skip Rules

**Source fields** — when a `{Field}` placeholder is empty:

| Condition               | Default | With `--allow-partial-source` |
| ----------------------- | ------- | ----------------------------- |
| All source fields empty | Skip    | Skip                          |
| Some empty, some filled | Skip    | Process (empty → `""`)        |
| All filled              | Process | Process                       |

**Target fields** — resume (already-filled notes are skipped):

| Condition                   | Default       | With `--force`      |
| --------------------------- | ------------- | ------------------- |
| All target fields non-empty | Skip (resume) | Process (overwrite) |
| Any target field empty      | Process       | Process             |

No checkpoint file — resume is field-based.

## For Developers

- CLI: [`ankiman/cli.py`](ankiman/cli.py)
- Anki integration: [`ankiman/anki.py`](ankiman/anki.py)
- LLM integration: [`ankiman/llm.py`](ankiman/llm.py)
- Config and models: [`ankiman/config.py`](ankiman/config.py)
- Secret storage: [`ankiman/secrets/`](ankiman/secrets/)
- Package config: [`pyproject.toml`](pyproject.toml)
