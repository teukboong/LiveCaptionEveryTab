# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows semantic versioning once releases are tagged.

## [Unreleased]

### Added

- Added a manifest-order content-script load smoke test so extension split changes fail fast before runtime.
- Added a reproducible speaker fixture bench for local calibration of speaker-label thresholds.
- Added contribution guidelines and issue templates for bug reports and feature requests.

### Changed

- Split the large content script into focused caption overlay, caption scheduler, page translator, and residual router files while preserving classic content-script load behavior.
- Split bridge helpers into smaller modules for text handling, policy, prompts, page markers, term memory, model runtime, ASR, and translation.
- Improved speaker labeling by using session-centered speaker similarity and calibrated default thresholds from local fixture measurements.
- Pinned CUDA ASR model revisions and the llama.cpp installer reference for more reproducible setup.
- Moved the Apple Silicon ASR dependency from a main-branch Git pin to the released `mlx-audio==0.4.4` package.
- Updated GitHub Actions workflow actions to the Node 24 runtime generation.

### Fixed

- Preserved page translation and caption prompt behavior with regression tests during backend and extension refactors.
