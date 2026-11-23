from pathlib import Path


def test_render_blueprint_declares_angel_one_credentials_as_secrets():
    manifest = (
        Path(__file__).resolve().parents[1] / "render.yaml"
    ).read_text(encoding="utf-8")

    required = (
        "ANGEL_API_KEY",
        "ANGEL_CLIENT_ID",
        "ANGEL_PASSWORD",
        "ANGEL_TOTP_SECRET",
    )
    for key in required:
        entry = f"      - key: {key}\n        sync: false"
        assert manifest.count(entry) == 1, (
            f"{key} must be declared exactly once as a non-synced secret"
        )
