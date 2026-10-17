# classifier/ — Stream B

A reversibility classifier with four labels: reversible, reversible_noisy, compensable, irreversible.

- `data/`: dataset builder output, made from APIs.guru operations plus our MCP tools (SFT JSONL with `prompt` and `answer`). Raw data is gitignored.
- `configs/`: veri.studio training configs (`train.toml`)
- `eval/`: held-out set and eval report (target >=90%)
