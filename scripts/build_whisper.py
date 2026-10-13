import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
prefix = Path(subprocess.check_output(["brew", "--prefix", "whisper.cpp"], text=True).strip())
ggml_prefix = Path(subprocess.check_output(["brew", "--prefix", "ggml"], text=True).strip())
output = ROOT / ".runtime/libvoice_whisper.dylib"
output.parent.mkdir(exist_ok=True)
temporary = output.with_suffix(".next.dylib")
subprocess.run(
    [
        "clang++",
        "-std=c++17",
        "-O3",
        "-dynamiclib",
        str(ROOT / "native/whisper_bridge.cpp"),
        "-I" + str(prefix / "include"),
        "-I" + str(ggml_prefix / "include"),
        "-L" + str(prefix / "lib"),
        "-L" + str(ggml_prefix / "lib"),
        "-lwhisper",
        "-lggml",
        "-lggml-base",
        '-DGGML_BACKEND_DIR="' + str(ggml_prefix / "libexec") + '"',
        "-Wl,-rpath," + str(prefix / "lib"),
        "-Wl,-rpath," + str(ggml_prefix / "lib"),
        "-o",
        str(temporary),
    ],
    check=True,
)
os.replace(temporary, output)
print("Native STT bridge built:", output)
