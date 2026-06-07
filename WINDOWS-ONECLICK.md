# Live Caption Windows One-Click

Run `install-windows-oneclick.bat` as administrator from the extracted archive.

The installer:

- installs Chrome if it is missing;
- installs or verifies Ubuntu WSL2;
- copies this project into `/root/LiveCaptionEveryTab` inside WSL;
- installs Ubuntu packages, a Python venv, and a CUDA llama.cpp build;
- downloads the Gemma 4 E4B QAT GGUF translation model;
- registers the Windows native messaging host so the popup Bridge Start/Stop buttons control the WSL CUDA stack;
- copies the extension to `%LOCALAPPDATA%\Hesperides\LiveCaption\extension`;
- creates `Live Caption Chrome.cmd` on the desktop.

Notes:

- NVIDIA's Windows driver must already expose `nvidia-smi` inside WSL. If it does not, install/update the driver,
  reboot, and rerun the installer.
- Gemma may require Hugging Face license acceptance. If the download fails, accept the model terms, set `HF_TOKEN`,
  and rerun the installer.
- The installer defaults to the 8 GB-friendly E4B translation path and popup-switched ASR, keeping one ASR engine
  resident at a time.
