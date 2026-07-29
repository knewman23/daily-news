# Observed `claude -p` contract

Probed 2026-07-28 with `claude` 2.1.x on this machine. `summarize.py` is written
against what is recorded here, not against assumption.

## Invocation

```bash
claude -p --output-format json --strict-mcp-config --model claude-opus-5
```

Prompt goes on **stdin**, not argv. Confirmed working — this matters because a
day of transcripts is far larger than a comfortable argv limit.

`--strict-mcp-config` skips loading this machine's MCP servers. They are
irrelevant to summarizing and each one adds startup latency and tokens.

`--model` pins the model. Without it the CLI inherits whatever the session
default is, so an unattended daily job would silently change behavior when that
default moves. `claude-opus-5` is the current default and the most capable
option; `claude-sonnet-5` is a cheaper alternative that would handle this task
fine — changing it is a one-line edit in `summarize.py`.

## Output shape

Single-line JSON on stdout. The model's answer is a **string** at `.result`:

```json
{
  "is_error": false,
  "result": "{\"topics\":[...]}",
  "stop_reason": "end_turn",
  "usage": { "input_tokens": 2, "cache_creation_input_tokens": 10963,
             "cache_read_input_tokens": 15880, "output_tokens": 9 },
  "total_cost_usd": 0.117805,
  "type": "result"
}
```

So parsing is two steps: `json.loads(stdout)["result"]`, then `json.loads()` of
that string. In the probe the inner payload came back as bare JSON with no code
fence, but tolerate a ```` ```json ```` wrapper anyway — it is one cheap regex
and the failure mode without it is a crashed daily run.

## Failure signature

| Condition | Signal |
|---|---|
| Success | exit 0, `is_error: false` |
| CLI usage error (bad flag) | exit 1, message on stderr, no JSON on stdout |
| Model-level error | exit 0 with `is_error: true` — check the field, not just the exit code |

Checking the exit code alone is not enough: a model-level failure still exits 0.

## Cost and token overhead

Every call carries ~11k cache-creation + ~16k cache-read tokens of base system
prompt regardless of how small the prompt is. The trivial probe reported
`total_cost_usd` around $0.12. On a subscription that is notional rather than
billed, but it does draw against usage limits — one call per day is fine, and
this is the reason `summarize.py` makes exactly one call for the whole day
rather than one per transcript.

## De-dup sanity check

Probed with three fake transcripts where two covered the same story. The model
collapsed them into one topic listing both handles and left the third as its own
topic, unprompted beyond the schema instruction. The same-day de-dup requirement
needs no special prompt engineering beyond stating the rule.
