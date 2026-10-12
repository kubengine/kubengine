import pytest

from core.registry_auth import RegistryCredentialsError, format_registry_auth


def test_format_registry_auth_returns_complete_credentials():
    assert format_registry_auth("robot", "secret") == "robot:secret"


@pytest.mark.parametrize(
    "username,password,missing_field",
    [
        ("", "secret", "registry.username"),
        ("   ", "secret", "registry.username"),
        ("robot", "", "registry.password"),
    ],
)
def test_format_registry_auth_rejects_incomplete_credentials(
    username, password, missing_field
):
    with pytest.raises(RegistryCredentialsError) as caught:
        format_registry_auth(username, password)

    assert missing_field in str(caught.value)
    assert "重启服务" in str(caught.value)
