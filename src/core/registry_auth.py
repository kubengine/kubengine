"""Registry credential validation shared by non-interactive commands."""


class RegistryCredentialsError(ValueError):
    """Reject credentials that would require an interactive prompt."""


def format_registry_auth(username: str, password: str) -> str:
    """Return ``username:password`` or reject incomplete credentials.

    ``ctr`` prompts on a terminal when ``--user`` contains a username
    but an empty password. Background jobs have no terminal, so that
    input makes containerd panic instead of returning a useful error.
    """
    if not username.strip():
        raise RegistryCredentialsError(
            "Harbor 推送用户名未配置，请在当前生效的 application.yaml 中配置 "
            "registry.username 并重启服务"
        )
    if not password:
        raise RegistryCredentialsError(
            "Harbor 推送密码未配置，请在当前生效的 application.yaml 中配置 "
            "registry.password 并重启服务"
        )
    return f"{username}:{password}"
