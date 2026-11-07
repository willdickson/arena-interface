"""Run every example against the fake device (no hardware required).

Each example runs in its own temporary directory, so files it writes do not
end up in the repository.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
SCRIPTS = sorted(p for p in EXAMPLES_DIR.glob("[0-9][0-9]_*.py"))
ENV = {**os.environ, "MPLBACKEND": "Agg"}


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
        env=ENV,
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_example_runs_simulated(script: Path, tmp_path: Path) -> None:
    result = _run([str(script), "--simulate"], tmp_path)
    assert result.returncode == 0, result.stderr


def test_live_plot_runs_simulated(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    script = EXAMPLES_DIR / "plotting" / "live_plot.py"
    result = _run([str(script), "--simulate", "--seconds", "1"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "analog_plot.png").exists()


def test_notebook_runs(tmp_path: Path) -> None:
    nbformat = pytest.importorskip("nbformat")
    nbclient = pytest.importorskip("nbclient")
    pytest.importorskip("ipykernel")
    path = EXAMPLES_DIR / "notebooks" / "walkthrough.ipynb"
    notebook = nbformat.read(path, as_version=4)
    client = nbclient.NotebookClient(
        notebook,
        timeout=60,
        kernel_name="python3",
        resources={"metadata": {"path": str(tmp_path)}},
    )
    client.execute()


@pytest.mark.parametrize("script", ["webcam.py", "movie.py"])
def test_camera_examples_run_simulated(script: str, tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    path = EXAMPLES_DIR / "camera" / script
    args = [str(path), "--simulate", "--no-gui", "--seconds", "1", "--rotate", "90"]
    result = _run(args, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "frames" in result.stdout


def test_movie_example_plays_a_file(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    movie = tmp_path / "clip.avi"
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(movie), fourcc, 25, (320, 240))
    for i in range(10):
        writer.write(np.full((240, 320, 3), i * 25, dtype=np.uint8))
    writer.release()
    script = EXAMPLES_DIR / "camera" / "movie.py"
    args = [str(script), str(movie), "--simulate", "--no-gui", "--once"]
    result = _run(args, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "25 frames/s" in result.stdout
