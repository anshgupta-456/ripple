from app.security.auth import issue_dev_token


def H(persona: str, tenant: str = "demo") -> dict[str, str]:
    """Authorization header for a dev persona (priya=analyst, sam=admin, meena=viewer)."""
    return {"Authorization": f"Bearer {issue_dev_token(persona, tenant)[0]}"}


ANALYST = H("priya")
ADMIN = H("sam")
VIEWER = H("meena")
