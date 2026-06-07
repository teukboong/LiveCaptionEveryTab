# Live Caption Windows One-Click

Run `install-windows-oneclick.bat` as administrator from the extracted archive.

The installer:

- installs Chrome if it is missing;
- installs or verifies Ubuntu WSL2;
- copies this project into `/root/LiveCaptionEveryTab` inside WSL;
- installs Ubuntu packages, a Python venv, and a CUDA llama.cpp build;
- downloads the chosen tier's translation GGUF (default mid / Gemma-4 E4B QAT — one tier, not all);
- registers the Windows native messaging host so the popup Bridge Start/Stop buttons control the WSL CUDA stack;
- copies the extension to `%LOCALAPPDATA%\LiveCaptionEveryTab\extension`;
- creates `Live Caption Chrome.cmd` on the desktop.

Notes:

- NVIDIA's Windows driver must already expose `nvidia-smi` inside WSL. If it does not, install/update the driver,
  reboot, and rerun the installer.
- Gemma may require Hugging Face license acceptance. If the download fails, accept the model terms, set `HF_TOKEN`,
  and rerun the installer.
- The installer defaults to the mid (E4B) translation tier and popup-switched ASR (one engine resident at a
  time). Switch tiers (full/lite) later from the popup or by rerunning with a tier arg.
