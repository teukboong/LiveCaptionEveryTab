# Contributing

Thanks for helping improve Live Caption Every Tab. Keep changes small, tested, and easy to review.

## Development Setup

Run the project setup first:

```bash
./setup.sh
```

Use the generated virtual environment unless you have a specific reason not to. The extension tests are zero-install Node scripts; do not add a Node dependency tree for routine checks.

## Verification

`check.sh` is the single local gate:

```bash
bash check.sh
```

It runs model-free bridge tests, extension syntax checks, protocol/runtime tests, and `tools/quality_gate.py`. Live GPU/CUDA/browser verification is separate and should be called out explicitly when it was not run.

## Code Boundaries

Follow the existing module boundaries. `tools/quality_gate.py` enforces the most important ones, including bridge split rules, extension content-script order, and file-size caps.

Keep commits path-scoped: do not mix unrelated bridge, extension, CUDA, documentation, or workflow changes in one commit.

## Test Style

The default tests are plain Python and Node scripts. Keep them model-free and deterministic. Add focused characterization tests near the behavior being changed; avoid weakening existing assertions just to make a refactor pass.

## Pull Requests

Include a short summary, the exact validation commands you ran, and any live/manual checks that still need to happen. Never claim CUDA, browser, or real-media behavior was verified unless you actually ran that path.
