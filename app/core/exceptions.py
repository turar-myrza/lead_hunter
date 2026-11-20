"""Safe application errors for the Bitrix24 integration."""


class Bitrix24Error(Exception):
    """Base class for expected Bitrix24 integration errors."""


class Bitrix24ConfigurationError(Bitrix24Error):
    """Raised when required local configuration is missing or invalid."""


class Bitrix24NetworkError(Bitrix24Error):
    """Raised when Bitrix24 cannot be reached over the network."""


class Bitrix24AuthorizationError(Bitrix24Error):
    """Raised when webhook credentials or permissions are rejected."""


class Bitrix24APIError(Bitrix24Error):
    """Raised when Bitrix24 reports a non-authorization API error."""


class Bitrix24EmptyResponseError(Bitrix24Error):
    """Raised when Bitrix24 returns no response body."""


class Bitrix24UnexpectedResponseError(Bitrix24Error):
    """Raised when a Bitrix24 response has an unexpected structure."""


class Bitrix24ReadOnlyViolationError(Bitrix24Error):
    """Raised if code attempts to call a method outside the read-only allowlist."""
