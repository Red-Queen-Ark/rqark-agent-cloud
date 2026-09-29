from fastapi import Header
from rqark_app.cloud.client import DataCloudError, RQArkDataCloudClient
from rqark_app.types.cloud import CloudConfig

from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL


class AuthError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not token:
        raise AuthError(401, 'UNAUTHORIZED', 'Authentication credentials were not provided.')
    return token


async def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    if authorization is None:
        raise AuthError(401, 'UNAUTHORIZED', 'Authentication credentials were not provided.')
    config = CloudConfig(url=DATA_CLOUD_URL, api_key=_bearer(authorization))
    try:
        async with RQArkDataCloudClient(config, timeout=DATA_CLOUD_TIMEOUT) as client:
            return await client.me()
    except DataCloudError as error:
        if error.status_code >= 400:
            raise AuthError(error.status_code, error.code, error.message) from error
        raise AuthError(502, 'INTERNAL', 'Cannot reach data cloud.') from error
