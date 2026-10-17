# classifier/ — Stream B

A classifier for MCP tools that have no entry in `contracts/reversibility_policy.yaml`, which in practice means tools from MCP servers placed behind Mulligan. It reads a tool's name, description and input schema and returns a label (read_only, reversible, reversible_noisy, compensable, irreversible) with a confidence.

Built-in tools never reach it, because the policy table is the authority for them. The gateway lets an unknown tool's call through only when the label is `read_only` at or above `CLASSIFIER_MIN_CONFIDENCE`. Any other label, a timeout or an error holds the call for approval.

- `data/`: dataset builder output, made from APIs.guru operations turned into MCP-style tool schemas (SFT JSONL with `prompt` and `answer`). Our built-in tools go in the eval set only. Raw data is gitignored.
- `configs/`: veri.studio training configs (`train.toml`)
- `eval/`: held-out set and eval report: accuracy (target >=90%) and unsafe misses, meaning a write tool labeled `read_only` at or above the threshold (target 0)
