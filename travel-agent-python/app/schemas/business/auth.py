"""认证域线级契约（G-1.1 从 app/api/business/auth.py 就地迁入）。

职责：
- LoginBody/RegisterBody：注册与登录请求体，校验规则与文案逐字保留；
- UserInfoVO：user_service.to_vo 的唯一模型源（路由返回 {"user": ...} 的 data 形状）；
- LoginData：登录响应 data 的声明式契约（凭据只进 HttpOnly Cookie，body 不含 token）。

实现要点：
- 校验器（用户名 3-32、密码 6-24、字符集、昵称长度）原样迁入，错误文案是
  前端断言过的契约，禁止顺手改写；
- UserInfoVO 由 to_vo 在运行时构造（模型即真相），导出物随模型漂移会被
  CI drift 门禁拦下。

依赖：
- pydantic；无内部依赖。
"""

import re

from pydantic import BaseModel, field_validator

USERNAME_RE = re.compile(r"^[A-Za-z0-9_一-龥]+$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9!@#$%^&*_\-]+$")


class LoginBody(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _username_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("用户名不能为空")
        return value

    @field_validator("password")
    @classmethod
    def _password_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        return value


class RegisterBody(BaseModel):
    username: str
    password: str
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def _username_rules(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("用户名不能为空")
        if not (3 <= len(value) <= 32):
            raise ValueError("用户名长度需在 3-32 之间")
        if not USERNAME_RE.match(value):
            raise ValueError("用户名仅支持中英文、数字与下划线")
        return value

    @field_validator("password")
    @classmethod
    def _password_rules(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("密码不能为空")
        if not (6 <= len(value) <= 24):
            raise ValueError("密码长度需在 6-24 之间")
        if not PASSWORD_RE.match(value):
            raise ValueError("密码包含非法字符")
        return value

    @field_validator("nickname")
    @classmethod
    def _nickname_rules(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 32:
            raise ValueError("昵称长度需在 32 以内")
        return value


class UserInfoVO(BaseModel):
    """`user_service.to_vo` 的形状；与 Java 侧 UserVO 字段一一对应。"""

    id: int
    username: str
    nickname: str | None
    phone: str | None
    role: str | None


class LoginData(BaseModel):
    """`POST /api/auth/login` 响应 data：{"user": UserInfoVO}。

    body 不回传 JWT：凭据只进 HttpOnly Cookie（TA_AUTH）。
    """

    user: UserInfoVO
