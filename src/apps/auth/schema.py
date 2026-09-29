from pydantic import BaseModel


class LoginRequest(BaseModel):
    login: str
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
