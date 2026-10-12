import io
import json
import tarfile

import pytest

from web.api.artifacts import _inspect_image_archive


def _add_bytes(archive, name, content):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    archive.addfile(info, io.BytesIO(content))


def _add_json(archive, name, value):
    _add_bytes(archive, name, json.dumps(value).encode())


def test_accepts_oci_image_archive(tmp_path):
    path = tmp_path / "images.tar"
    with tarfile.open(path, "w") as archive:
        _add_json(archive, "oci-layout", {"imageLayoutVersion": "1.0.0"})
        _add_json(archive, "index.json", {"manifests": []})

    assert _inspect_image_archive(str(path)) == {}


def test_accepts_docker_image_archive(tmp_path):
    path = tmp_path / "images.tar"
    with tarfile.open(path, "w") as archive:
        _add_json(archive, "manifest.json", [])

    assert _inspect_image_archive(str(path)) == {}


def test_rejects_helm_chart_with_actionable_error(tmp_path):
    path = tmp_path / "chart.tgz"
    with tarfile.open(path, "w:gz") as archive:
        _add_bytes(archive, "flink/Chart.yaml", b"apiVersion: v2\n")

    with pytest.raises(RuntimeError, match="Helm Chart.*images.tar"):
        _inspect_image_archive(str(path))


def test_rejects_image_layout_wrapped_in_directory(tmp_path):
    path = tmp_path / "wrapped.tar"
    with tarfile.open(path, "w") as archive:
        _add_json(
            archive,
            "bundle/oci-layout",
            {"imageLayoutVersion": "1.0.0"},
        )
        _add_json(archive, "bundle/index.json", {"manifests": []})

    with pytest.raises(RuntimeError, match="额外目录.*归档根目录"):
        _inspect_image_archive(str(path))


def test_rejects_generic_tar_with_export_hint(tmp_path):
    path = tmp_path / "generic.tar"
    with tarfile.open(path, "w") as archive:
        _add_bytes(archive, "README.txt", b"not an image archive")

    with pytest.raises(RuntimeError, match="ctr images export"):
        _inspect_image_archive(str(path))
