# Ollama live preparation fixtures

Captured read-only from timmy on 2026-10-26, Ollama 0.40.11, after the first
IMPR-1426 deployment failed before host adoption. These are model metadata,
not inference requests or responses.

- `gemma-live-show.json`: `/api/show` for `gemma4:vlm`, with only its final
  newline normalized by repository hooks. Its multiline TEMPLATE display
  reproduced `ValueError: No closing quotation` in the old `blob_ids()` parser.
- `gemma-live-manifest.json`: exact bytes from the model-store blob identified
  by `/api/tags`; SHA-256
  `801854f4128b59d3194d829fb061ea4389390f29c98a3b0047a1ef839b710c33`.
  The model and projector identities match the declared Gemma pins.

Pinned upstream contracts: [content-addressed manifest storage](https://github.com/ollama/ollama/blob/v0.40.11/manifest/manifest.go),
[blob paths](https://github.com/ollama/ollama/blob/v0.40.11/manifest/paths.go),
and [model layer consumption](https://github.com/ollama/ollama/blob/v0.40.11/server/images.go).
The tests retain the real multiline display and exercise the structured manifest
without calling a model or requiring live systemd state.
