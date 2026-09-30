"""Configuration for one set of Reolink cameras."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ReolinkCameraItemConfig(BaseModel):
    """Connection settings for one Reolink camera or NVR channel."""

    model_config = ConfigDict(extra="forbid")

    host: str
    username: str
    password: SecretStr
    # None picks the protocol's default port: 443 with HTTPS, 80 without.
    port: int | None = None
    use_https: bool = True
    channel: int = 0
    name: str | None = None
    # Skip FLV streaming and download recordings directly, for cameras whose
    # TLS makes FLV fail.
    prefer_download: bool = False

    @property
    def effective_port(self) -> int:
        """The configured port, or the protocol's default."""
        if self.port is not None:
            return self.port
        return 443 if self.use_https else 80


class ReolinkConfig(BaseModel):
    """The cameras one Reolink instance serves, keyed by camera id."""

    model_config = ConfigDict(extra="forbid")

    # Deployments usually supply these through the REOLINK_CAMERAS environment
    # variable (JSON), which fills the default instance. An instance with no
    # cameras is not started.
    cameras: dict[str, ReolinkCameraItemConfig] = Field(default_factory=dict)
