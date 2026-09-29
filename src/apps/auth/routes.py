from fastapi import APIRouter, Depends

from src.middleware.auth import get_current_user

router = APIRouter(tags=['Auth'], prefix='/auth')


@router.get('/me')
async def me(user: dict = Depends(get_current_user)) -> dict:
    return user
