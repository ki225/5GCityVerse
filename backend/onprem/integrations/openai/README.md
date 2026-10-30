# OpenAI Responses API adapter

The on-prem application uses OpenAI for server-side AI inference. It does not
deploy a local model and does not require AWS credentials.

## Configuration

Set `OPENAI_API_KEY` in the backend environment. `OPENAI_MODEL` is optional and
defaults to `gpt-5.5`. Other model IDs are rejected before any API call. Optional runtime controls are:

- `OPENAI_TIMEOUT_SECONDS` (default `45`)
- `OPENAI_MAX_OUTPUT_TOKENS` (default `4096`)
- `OPENAI_MAX_RETRIES` (default `1`, maximum `3`)
- `OPENAI_RETRY_BACKOFF_SECONDS` (default `1`)

Never expose the key through a `VITE_*` variable or commit it to the repository.
Prompts and scenario context are sent to `https://api.openai.com/v1/responses`,
so AI inference requires outbound HTTPS connectivity.

## Standalone check

```powershell
$env:OPENAI_API_KEY = '<your key>'
$env:OPENAI_MODEL = 'gpt-5.5'
python -m backend.onprem.integrations.openai.openai_client --prompt "Explain 5G network slicing in one sentence."
```

Unit tests mock the network and do not consume API credits:

```powershell
python -m unittest test.subsystems.onprem.test_openai_client -v
```
