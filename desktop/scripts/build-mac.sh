#!/usr/bin/env bash
set -euo pipefail

arch="${1:-}"
case "$arch" in
  arm64)
    python_arch=aarch64
    python_sha=ba32036ad8f4c434d97a1ee7feef8b7edc0fac16777639b97e39c74a108e4440
    memory_packages=('numpy==2.5.4' 'torch==2.14.1' 'transformers==5.20.1' 'sentence-transformers==5.7.0')
    ;;
  x64)
    python_arch=x86_64
    python_sha=9686ddd8639b855905447ea4a16eb54a804a9ef12afbe7c9ecb1412d9c1fbc98
    # The last macOS Intel PyTorch wheel is 2.2.2; Transformers 5 requires 2.6.
    memory_packages=('numpy==1.26.4' 'torch==2.2.2' 'transformers==4.49.0' 'sentence-transformers==3.4.1')
    ;;
  *)
    printf 'Usage: %s arm64|x64\n' "$0" >&2
    exit 2
    ;;
esac

desktop_root="$(cd "$(dirname "$0")/.." && pwd)"
repo_root="$(cd "$desktop_root/.." && pwd)"
if [[ ! -f "$desktop_root/icon.icns" ]]; then
  bash "$desktop_root/scripts/create-icon.sh"
fi
package_root="$repo_root/.context/mac-package"
stage="$package_root/staging-$arch"
asset="cpython-3.12.15%2B20261013-$python_arch-apple-darwin-install_only.tar.gz"
archive="$package_root/$python_arch-python-3.12.15.tar.gz"
url="https://github.com/astral-sh/python-build-standalone/releases/download/20261013/$asset"

mkdir -p "$package_root" "$stage"
if [[ ! -f "$archive" ]]; then
  curl --fail --location --retry 3 --output "$archive" "$url"
fi
actual_sha="$(shasum -a 256 "$archive" | awk '{print $1}')"
if [[ "$actual_sha" != "$python_sha" ]]; then
  printf 'Python archive checksum mismatch for %s\n' "$arch" >&2
  exit 1
fi
if [[ ! -x "$stage/python/bin/python3.12" ]]; then
  tar -xzf "$archive" -C "$stage"
fi
python="$stage/python/bin/python3.12"
if [[ "$(uname -m)" == arm64 && "$arch" == x64 ]]; then
  runner=(arch -x86_64)
else
  runner=(env)
fi
can_run_python=1
if [[ "$arch" == x64 && "$(uname -m)" == arm64 ]] && ! arch -x86_64 "$python" --version >/dev/null 2>&1; then
  can_run_python=0
fi
if [[ "$can_run_python" == 1 ]]; then
  "${runner[@]}" "$python" -m ensurepip --upgrade
  "${runner[@]}" "$python" -m pip install --disable-pip-version-check --no-cache-dir \
    'aiohttp==3.14.5' 'Pillow==12.3.0' 'imageio-ffmpeg==0.6.0' \
    'jsonschema==4.26.0' 'keyring==25.7.0' 'cryptography==46.0.7' \
    'mem0ai==2.0.18' "${memory_packages[@]}"

  model_cache="$package_root/model-cache"
  OLIVIA_MEMORY_ENABLED=1 OLIVIA_MEMORY_ROOT="$package_root/memory" \
    OLIVIA_MEMORY_EMBEDDING_CACHE="$model_cache" PYTHONPATH="$repo_root/client" \
    "${runner[@]}" "$python" -c 'from mem0_memory import load_mem0_config, verified_embedding_cache; from mem0_embedding_install import Mem0EmbeddingInstaller; c=load_mem0_config(); r=Mem0EmbeddingInstaller(c).install(); assert r.status in ("APPLIED", "NOOP") and verified_embedding_cache(c), r.reason_code'
  mkdir -p "$stage/memory-model-cache"
  rsync -a "$model_cache/" "$stage/memory-model-cache/"
else
  printf 'Rosetta is unavailable; using the existing verified x64 Python staging.\n' >&2
  if [[ ! -d "$stage/python/lib/python3.12/site-packages/aiohttp" || ! -d "$stage/memory-model-cache" ]]; then
    printf 'x64 staging is incomplete; install Rosetta 2 and rerun this build.\n' >&2
    exit 1
  fi
fi

mkdir -p "$stage/backend"
rsync -a --delete --delete-excluded "$repo_root/client/" "$stage/backend/" \
  --exclude='.*' \
  --exclude='*.egg-info/' \
  --exclude='.venv/' --exclude='.olivia_data/' --exclude='__pycache__/' \
  --exclude='tests/' --exclude='docs/' --exclude='tools/' --exclude='scripts/' \
  --exclude='*.pyc' --exclude='*.png' --exclude='*.jpg' --exclude='*.jpeg' \
  --exclude='*.gif' --exclude='*.webp' --exclude='*.mp4' --exclude='*.wav' \
  --exclude='*.mp3' --exclude='*.zip' --exclude='*.7z' --exclude='*.exe'
rsync -a --delete "$repo_root/client/runtime/letter_stickers/" "$stage/backend/runtime/letter_stickers/"
mkdir -p "$stage/backend/installer/assets"
cp "$repo_root/client/installer/assets/wechat-payment.jpeg" "$stage/backend/installer/assets/wechat-payment.jpeg"
if [[ "$can_run_python" == 1 ]]; then
  "${runner[@]}" "$python" -c 'import aiohttp, imageio_ffmpeg, jsonschema, keyring, mem0, sentence_transformers; print("Python dependencies ready")'
  file "$python"
  "${runner[@]}" "$python" --version
else
  file "$python"
fi

ln -sfn "staging-$arch" "$package_root/current"
cd "$desktop_root"

# Sign the finished bundle, including its resources and nested executables.
# Apple Silicon rejects an executable whose linker signature was removed.
# A complete ad-hoc signature supports local use without a Developer ID;
# it does not make the app notarized or trusted for distribution.
if [[ -n "${OLIVIA_MAC_IDENTITY:-}" ]]; then
  export CSC_NAME="$OLIVIA_MAC_IDENTITY"
  export CSC_IDENTITY_AUTO_DISCOVERY=true
  npx electron-builder --mac dmg --"$arch" --publish never --config.mac.identity="$OLIVIA_MAC_IDENTITY"
else
  export CSC_IDENTITY_AUTO_DISCOVERY=false
  npx electron-builder --mac dmg --"$arch" --publish never \
    --config.mac.identity=- --config.mac.preAutoEntitlements=false \
    --config.mac.timestamp=none --config.mac.notarize=false
fi

app_dir=mac
[[ "$arch" == arm64 ]] && app_dir=mac-arm64
app_path="$desktop_root/dist/$app_dir/BSide Olivia.app"
codesign --verify --deep --strict "$app_path"
if [[ "$can_run_python" == 1 ]]; then
  ELECTRON_RUN_AS_NODE=1 "${runner[@]}" "$app_path/Contents/MacOS/BSide Olivia" \
    -e 'if (process.platform !== "darwin") process.exit(1); console.log("Packaged Electron starts:", process.arch)'
  "${runner[@]}" "$app_path/Contents/Resources/python/bin/python3.12" --version
else
  printf 'Runtime launch verification for %s requires Rosetta 2; signature verification passed.\n' "$arch" >&2
fi
