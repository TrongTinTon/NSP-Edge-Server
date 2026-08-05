# Part of T4 Core API. See LICENSE file for full copyright and licensing details.

from werkzeug.exceptions import BadRequest


class CoreApiBadRequest(BadRequest):
    """Base class for Core API client input / response shape errors (HTTP 400)."""


class CoreApiInvalidBody(CoreApiBadRequest):
    """Request body is not a JSON object (dict)."""


class CoreApiMissingData(CoreApiBadRequest):
    """Required request field(s) are missing or empty."""


class CoreApiInvalidData(CoreApiBadRequest):
    """Request field value is invalid."""


class CoreApiInvalidResponse(CoreApiBadRequest):
    """Server action set_api_response() was called with non-dict data."""


def ensure_dict(data, *, message=None):
    """Raise CoreApiInvalidBody when *data* is not a dict."""
    if not isinstance(data, dict):
        raise CoreApiInvalidBody(
            message or 'Request body must be a JSON object (dict).'
        )
    return data


def require_fields(data, fields, *, message=None):
    """Raise CoreApiMissingData when any required field is missing or empty."""
    ensure_dict(data)
    missing = [
        field for field in fields
        if field not in data or data[field] in (None, '')
    ]
    if missing:
        raise CoreApiMissingData(
            message or 'Missing required field(s): %s' % ', '.join(missing)
        )
    return data
