from fastapi import APIRouter

router = APIRouter(tags=['Ping'], prefix='/ping')


@router.get('')
async def ping():
    return {'message': 'pong'}
