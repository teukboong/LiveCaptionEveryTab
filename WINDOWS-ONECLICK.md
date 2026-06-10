# Live Caption Windows One-Click

Run `install-windows-oneclick.bat` as administrator from the extracted archive.

The installer:

- installs Chrome if it is missing;
- installs or verifies Ubuntu WSL2;
- copies this project into `/root/LiveCaptionEveryTab` inside WSL;
- installs Ubuntu packages, a Python venv, and a CUDA llama.cpp build;
- downloads one translation GGUF (default Gemma-4 E4B QAT — one model, not all);
- registers the Windows native messaging host so the popup Bridge Start/Stop buttons control the WSL CUDA stack;
- copies the extension to `%LOCALAPPDATA%\LiveCaptionEveryTab\extension`;
- creates `Live Caption Chrome.cmd` on the desktop.

Notes:

- NVIDIA's Windows driver must already expose `nvidia-smi` inside WSL. If it does not, install/update the driver,
  reboot, and rerun the installer.
- Gemma may require Hugging Face license acceptance. If the download fails, accept the model terms, set `HF_TOKEN`,
  and rerun the installer.
- The installer defaults to the Gemma-4 E4B translation model and popup-switched ASR (one engine resident at a
  time). The popup is now a model dropdown (gemma-26b / gemma-e4b / gemma-e2b · pick or Auto), not a tier picker.
  Neither this installer nor `windows\install-oneclick.ps1` takes a model argument; to pick a different
  translation model at install time, set the WSL-side env var `LCC_LM_TIER` (`full`=26B / `mid`=E4B / `lite`=E2B,
  read by `bridge/cuda/install_cuda_wsl.sh`) before running, or just rerun the installer.
