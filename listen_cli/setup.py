"""First-run setup and dependency checking."""

import os
import sys
import subprocess
import platform
import json
from pathlib import Path
from typing import Optional


def check_portaudio() -> bool:
    """Check if PortAudio is installed."""
    system = platform.system()

    if system == "Darwin":  # macOS
        # Check for Homebrew's portaudio
        result = subprocess.run(
            ["brew", "list", "portaudio"],
            capture_output=True,
            text=True
        )
        return result.returncode == 0

    elif system == "Linux":
        # Check for portaudio dev headers
        checks = [
            ["dpkg", "-l", "portaudio19-dev"],  # Debian/Ubuntu
            ["rpm", "-q", "portaudio-devel"],   # Fedora/RHEL
            ["pacman", "-Q", "portaudio"],      # Arch
        ]
        for check_cmd in checks:
            try:
                result = subprocess.run(check_cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    return True
            except FileNotFoundError:
                continue
        return False

    elif system == "Windows":
        # Windows usually has precompiled wheels
        return True

    return False


def install_portaudio() -> bool:
    """Attempt to install PortAudio with user confirmation."""
    system = platform.system()

    print("\n" + "="*50)
    print("🎤 Audio Dependencies Required")
    print("="*50)
    print("\nlisten-cli needs PortAudio for microphone access.")
    print("This is a one-time system setup.\n")

    install_cmd = None
    if system == "Darwin":
        install_cmd = "brew install portaudio"
        print(f"  Command: {install_cmd}")
    elif system == "Linux":
        install_cmd = "sudo apt-get install portaudio19-dev python3-pyaudio"
        print(f"  Command: {install_cmd}")
        print("  (For other distros, install portaudio-devel)")

    if not install_cmd:
        print("Please install PortAudio manually for your system.")
        return False

    response = input("\nInstall automatically? [y/N]: ").strip().lower()
    if response != 'y':
        print("\nTo install manually, run:")
        print(f"  {install_cmd}")
        return False

    print("\nInstalling...")
    result = subprocess.run(install_cmd, shell=True)
    return result.returncode == 0


def check_assemblyai_import() -> bool:
    """Check if AssemblyAI extras can be imported."""
    try:
        import assemblyai.extras
        return True
    except ImportError:
        return False


def get_config_dir() -> Path:
    """Get the configuration directory for listen-cli."""
    if os.name == 'nt':  # Windows
        config_home = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
    else:  # Unix-like
        config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))

    config_dir = config_home / 'listen-cli'
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def has_run_setup() -> bool:
    """Check if setup has been run before."""
    config_file = get_config_dir() / 'setup.json'
    if not config_file.exists():
        return False

    try:
        with open(config_file) as f:
            config = json.load(f)
            return config.get('setup_complete', False)
    except (json.JSONDecodeError, KeyError):
        return False


def mark_setup_complete():
    """Mark that setup has been completed."""
    config_file = get_config_dir() / 'setup.json'
    config = {}

    if config_file.exists():
        try:
            with open(config_file) as f:
                config = json.load(f)
        except json.JSONDecodeError:
            pass

    config['setup_complete'] = True
    config['setup_version'] = '1.0'

    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)


def setup_if_needed() -> bool:
    """Run setup checks and guide user through installation if needed.

    Returns True if everything is ready, False if setup failed.
    """
    # Skip setup if already completed
    if has_run_setup():
        # Quick check that imports still work
        if check_assemblyai_import():
            return True
        # Setup was marked complete but imports fail - continue with setup

    # Check if we can import AssemblyAI extras (which requires PyAudio)
    if check_assemblyai_import():
        mark_setup_complete()
        return True

    # Check if PortAudio is installed
    if not check_portaudio():
        print("\n⚠️  PortAudio not found")

        if not install_portaudio():
            print("\n❌ Setup incomplete. Please install PortAudio manually.")
            print("   Then reinstall listen-cli:")
            print("   pip install --force-reinstall listen-cli")
            return False

        # After installing PortAudio, we need to reinstall PyAudio
        print("\nReinstalling PyAudio with new system libraries...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-cache-dir", "pyaudio"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print("\n❌ Failed to install PyAudio")
            print("   You may need to install additional development tools:")
            print("   - Linux: sudo apt-get install python3-dev build-essential")
            print("   - macOS: xcode-select --install")
            return False

    # Final check
    if check_assemblyai_import():
        print("\n✅ Setup complete! Audio dependencies are ready.")
        mark_setup_complete()
        return True

    print("\n⚠️  Setup may be incomplete. Trying to continue anyway...")
    return True


def get_model_path() -> Optional[Path]:
    """Get the path to bundled models."""
    # Check if models are bundled with the package
    import listen_cli
    package_dir = Path(listen_cli.__file__).parent.parent
    model_dir = package_dir / "models" / "zipformer-en20m"

    if model_dir.exists():
        return model_dir

    # Check local development path
    local_dir = Path.cwd() / "models" / "zipformer-en20m"
    if local_dir.exists():
        return local_dir

    return None


def setup_models() -> bool:
    """Check and configure ASR models."""
    model_dir = get_model_path()

    if model_dir and model_dir.exists():
        # Set environment variables for sherpa if not already set
        if not os.getenv("LISTEN_SHERPA_TOKENS"):
            tokens = model_dir / "tokens.txt"
            encoder = model_dir / "encoder-epoch-99-avg-1.onnx"
            decoder = model_dir / "decoder-epoch-99-avg-1.onnx"
            joiner = model_dir / "joiner-epoch-99-avg-1.onnx"

            if all(f.exists() for f in [tokens, encoder, decoder, joiner]):
                os.environ["LISTEN_SHERPA_TOKENS"] = str(tokens)
                os.environ["LISTEN_SHERPA_ENCODER"] = str(encoder)
                os.environ["LISTEN_SHERPA_DECODER"] = str(decoder)
                os.environ["LISTEN_SHERPA_JOINER"] = str(joiner)
                print("✅ Local ASR models configured")
                return True

    # Models not found, but that's OK - can use AssemblyAI
    if os.getenv("ASSEMBLYAI_API_KEY"):
        print("ℹ️  Using AssemblyAI cloud ASR")
        return True

    print("\n⚠️  No ASR models configured")
    print("   Set ASSEMBLYAI_API_KEY or install local models")
    return True  # Don't block, let the app handle it