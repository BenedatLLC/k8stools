
from os.path import abspath, join, dirname
import k8stools

PROJECT_FILE=abspath(join(dirname(abspath(__file__)), '../pyproject.toml'))

def test_version_is_consistent():
    """Test that the version in the project file is consistent with the version in the code"""
    code_version = k8stools.__version__
    with open(PROJECT_FILE, "r") as f:
        for line in f:
            if line.strip().startswith("version"):
                project_version = line.split("=")[1].strip().strip('"').strip("'")
                break
        else:
            raise AssertionError("Version not found in pyproject.toml")

    assert code_version == project_version, f"Code version {code_version} does not match project version {project_version}"