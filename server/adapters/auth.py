from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExternalIdentity:
    openid: str
    nickname: str


class AuthProvider(Protocol):
    def exchange_code(self, code: str) -> ExternalIdentity: ...


class FakeAuthProvider:
    """Test-account login used until the WeChat provider is available."""

    def exchange_code(self, code: str) -> ExternalIdentity:
        if not code.startswith("test-"):
            raise ValueError("invalid fake login code")
        return ExternalIdentity(openid=f"fake:{code}", nickname="测试家长")
