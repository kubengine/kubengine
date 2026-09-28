from pathlib import Path
import runpy
import subprocess
import tarfile

import pytest


create_archive = runpy.run_path(str(Path(__file__).parents[1] / "scripts/create_source_archive.py"))["create_archive"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    files = {
        "config/application.example.yaml": "domain: example.invalid\n",
        "config/application.yaml": "synthetic-live-secret\n",
        "config/certs/test.key": "synthetic-private-key\n",
        "config/admin-user.token": "synthetic-token\n",
        "config/.k8s_deployment_state.json": "{}",
        "src/example.py": "pass\n",
        "src/accidentally-tracked.key": "synthetic-private-key\n",
        "README.md": "Synthetic release\n",
    }
    for name, data in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    return repo


def test_archive_contains_only_committed_release_files(release_repo, tmp_path):
    (release_repo / "src/untracked.py").write_text("synthetic untracked secret")
    (release_repo / "config/application.yaml").write_text("changed synthetic live secret")
    output = tmp_path / "sources.tar.gz"
    create_archive(release_repo, output, "kubengine-test")
    with tarfile.open(output) as archive:
        assert set(archive.getnames()) == {
            "kubengine-test/README.md", "kubengine-test/src/example.py",
            "kubengine-test/config/application.example.yaml",
        }
        assert b"synthetic" not in b"".join(archive.extractfile(m).read() for m in archive)


def test_archive_rejects_uncommitted_source_changes(release_repo, tmp_path):
    (release_repo / "src/example.py").write_text("changed\n")
    with pytest.raises(ValueError, match="uncommitted"):
        create_archive(release_repo, tmp_path / "out.tar.gz", "release")


def test_archive_rejects_symlinks_even_inside_allowlist(release_repo, tmp_path):
    (release_repo / "src/secret-link").symlink_to("../config/application.yaml")
    git(release_repo, "add", "src/secret-link")
    git(release_repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "symlink fixture")
    with pytest.raises(ValueError, match="regular tracked file"):
        create_archive(release_repo, tmp_path / "out.tar.gz", "release")
