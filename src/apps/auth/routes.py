from fastapi import APIRouter, Depends

from src.apps.auth import service
from src.apps.auth.schema import LoginRequest, TokenPair
from src.middleware.auth import get_current_user

router = APIRouter(tags=['Auth'], prefix='/auth')


@router.get('/me')
async def me(user: dict = Depends(get_current_user)) -> dict:
    return user


@router.post('/login')
async def login(body: LoginRequest) -> TokenPair:
    return await service.login(body.model_dump())
