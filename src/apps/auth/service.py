import httpx

from src.apps.auth.schema import TokenPair
from src.config import DATA_CLOUD_TIMEOUT, DATA_CLOUD_URL
from src.middleware.auth import AuthError


def _data_cloud_api_url() -> str:
    base = DATA_CLOUD_URL.rstrip('/')
    return base if base.endswith('/api') else f'{base}/api'


async def login(payload: dict[str, str]) -> TokenPair:
    try:
        async with httpx.AsyncClient(timeout=DATA_CLOUD_TIMEOUT) as client:
            response = await client.post(f'{_data_cloud_api_url()}/auth/login', json=payload)
    except httpx.HTTPError as error:
        raise AuthError(502, 'INTERNAL', 'Cannot reach data cloud.') from error
    if response.status_code >= 400:
        try:
            error = response.json().get('error') or {}
        except ValueError:
            error = {}
        raise AuthError(response.status_code, str(error.get('code') or 'UNKNOWN'), str(error.get('message') or f'data cloud answered {response.status_code}'))
    try:
        return TokenPair.model_validate(response.json())
    except ValueError as error:
        raise AuthError(502, 'INTERNAL', 'Data cloud returned an invalid response.') from error
