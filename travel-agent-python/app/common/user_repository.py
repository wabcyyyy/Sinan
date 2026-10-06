"""sys_user 数据访问（鉴权链路用，SQLAlchemy 实现）。

与 city_reference 等 agent 侧数据面的关键差别：那边查询失败返回空值以保证生成
链路不中断，**这里查询失败必须返回 None（fail-closed）**——数据库不可用时宁可
让请求 401，也不能把"查不到用户"当成"用户有效"。

迁移说明：业务层统一走 `app.db.session`（SQLAlchemy），不再用 pymysql 裸连接池——
否则同一张 sys_user 会有两条访问路径、两条软删语义，双跑期极易出现
"登录查得到、鉴权查不到"这类割裂行为（该缺陷在 M2 测试中被真实抓到过）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.db.models import SysUser
from app.db.session import session_scope

logger = logging.getLogger(__name__)


def find_by_username(username: str) -> dict[str, Any] | None:
    """按用户名取**未软删**用户；不存在或查询异常一律返回 None。

    软删条件由 `app.db.session` 的全局作用域追加（等价 Java @TableLogic）。
    """
    try:
        with session_scope() as session:
            user = session.execute(select(SysUser).where(SysUser.username == username)).scalar_one_or_none()
            if user is None:
                return None
            return {
                "id": user.id,
                "username": user.username,
                "nickname": user.nickname,
                "role": user.role,
                "status": user.status,
            }
    except Exception as exc:
        logger.error("find_by_username failed (fail-closed): %s", exc)
        return None
