import os
import random
import time

import httpx
import pytest

BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=60.0) as c:
        yield c


def _uid():
    return f"autotest_{random.randint(10000, 99999)}"


def register(client, username):
    r = client.post("/api/auth/register", json={"username": username, "password": "pass123", "nickname": "自动化"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 200
    return body


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "pass123"})
    assert r.status_code == 200
    assert "token" not in r.json()["data"], "登录响应不回传 JWT，凭据只进 HttpOnly Cookie"
    # 票从 TA_AUTH Cookie 取，再以 Bearer 头回放：Cookie 带 Secure，活栈跑在
    # http://127.0.0.1 上时 httpx 不会自动发送它，Cookie 通道由离线契约测试覆盖。
    return r.cookies.get("TA_AUTH")


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def generate(client, token, city="北京", days=2):
    """发起异步生成并轮询至完成，返回最终行程详情。

    生成接口先异步建壳返回 status=1（draft），逐日由后台填充后 status=2。
    测试需轮询等待，不能对同步建壳响应做完整 data 断言。
    """
    r = client.post(
        "/api/itinerary/generate",
        json={"city": city, "days": days, "persons": 2, "budget": 3000, "preferences": ["人文"]},
        headers=auth_headers(token),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 200
    detail = body["data"]
    itin_id = detail["id"]

    deadline = time.time() + 120
    while time.time() < deadline:
        d = client.get(f"/api/itinerary/{itin_id}", headers=auth_headers(token), timeout=60).json()
        assert d["code"] == 200
        d = d["data"]
        if d.get("status") == 2:
            return d
        time.sleep(2)
    raise AssertionError(f"等待行程生成超时: id={itin_id}, status={detail.get('status')}")


class TestAuth:
    def test_register_login_flow(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        assert token
        info = client.get("/api/user/info", headers=auth_headers(token)).json()
        assert info["code"] == 200
        assert info["data"]["username"] == u

    def test_duplicate_register_rejected(self, client):
        u = _uid()
        register(client, u)
        body = client.post("/api/auth/register", json={"username": u, "password": "x"}).json()
        assert body["code"] == 400

    def test_wrong_password_rejected(self, client):
        u = _uid()
        register(client, u)
        body = client.post("/api/auth/login", json={"username": u, "password": "wrong"}).json()
        assert body["code"] == 400

    def test_no_token_denied(self, client):
        r = client.get("/api/itinerary")
        assert r.status_code in (401, 403)


class TestItinerary:
    def test_generate_list_detail_delete(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        detail = generate(client, token)
        itin_id = detail["id"]
        assert detail["days"] == 2
        assert len(detail["dayList"]) == 2
        assert len(detail["budgetList"]) == 4

        lst = client.get("/api/itinerary", headers=auth_headers(token)).json()
        assert any(x["id"] == itin_id for x in lst["data"])

        d = client.get(f"/api/itinerary/{itin_id}", headers=auth_headers(token)).json()
        assert d["code"] == 200
        # 开放模式下单日行程项数量随 LLM 选点浮动，断言为合理下界而非固定值。
        assert len(d["data"]["dayList"][0]["items"]) >= 1

        r = client.delete(f"/api/itinerary/{itin_id}", headers=auth_headers(token)).json()
        assert r["code"] == 200
        lst2 = client.get("/api/itinerary", headers=auth_headers(token)).json()
        assert not any(x["id"] == itin_id for x in lst2["data"])

    def test_validation_rejected(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        body = client.post(
            "/api/itinerary/generate", json={"city": "北京", "days": 0, "persons": 2}, headers=auth_headers(token)
        ).json()
        assert body["code"] == 400

    def test_cross_user_isolation(self, client):
        u1, u2 = _uid(), _uid()
        register(client, u1)
        register(client, u2)
        t1 = login(client, u1)
        t2 = login(client, u2)
        detail = generate(client, t1)
        itin_id = detail["id"]
        body = client.get(f"/api/itinerary/{itin_id}", headers=auth_headers(t2)).json()
        assert body["code"] == 404

    def test_edit_flow_and_budget_recalc(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        detail = generate(client, token)
        itin_id = detail["id"]
        day_id = detail["dayList"][0]["dayId"]
        total0 = float(detail["totalAmount"])

        r = client.post(
            f"/api/itinerary/{itin_id}/items",
            json={"dayId": day_id, "itemType": "attraction", "poiName": "新增测试景点", "cost": 25.5},
            headers=auth_headers(token),
        ).json()
        assert r["code"] == 200
        assert float(r["data"]["totalAmount"]) == total0 + 25.5 * 2
        item_id = next(i["id"] for i in r["data"]["dayList"][0]["items"] if i["poiName"] == "新增测试景点")

        r = client.put(f"/api/itinerary/items/{item_id}", json={"cost": 30}, headers=auth_headers(token)).json()
        assert r["code"] == 200
        assert float(r["data"]["totalAmount"]) == total0 + 30 * 2

        ids = [i["id"] for i in r["data"]["dayList"][0]["items"]]
        r = client.put(
            f"/api/itinerary/{itin_id}/days/{day_id}/order", json=list(reversed(ids)), headers=auth_headers(token)
        ).json()
        assert r["code"] == 200
        assert [i["id"] for i in r["data"]["dayList"][0]["items"]] == list(reversed(ids))

        r = client.delete(f"/api/itinerary/items/{item_id}", headers=auth_headers(token)).json()
        assert r["code"] == 200
        assert float(r["data"]["totalAmount"]) == total0


class TestExport:
    def test_pdf_export_flow(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        detail = generate(client, token)
        itin_id = detail["id"]

        r = client.post(f"/api/export/pdf/{itin_id}", headers=auth_headers(token)).json()
        assert r["code"] == 200
        task_id = r["data"]["id"]
        assert r["data"]["status"] == "RUNNING"

        status = None
        for _ in range(30):
            time.sleep(1)
            r = client.get(f"/api/export/tasks/{task_id}", headers=auth_headers(token)).json()
            status = r["data"]["status"]
            if status in ("DONE", "FAILED"):
                break
        assert status == "DONE"

        resp = client.get(f"/api/export/download/{task_id}", headers=auth_headers(token))
        assert resp.status_code == 200
        assert resp.content[:5] == b"%PDF-"

    def test_export_cross_user_denied(self, client):
        u1, u2 = _uid(), _uid()
        register(client, u1)
        register(client, u2)
        t1 = login(client, u1)
        t2 = login(client, u2)
        detail = generate(client, t1)
        r = client.post(f"/api/export/pdf/{detail['id']}", headers=auth_headers(t1)).json()
        task_id = r["data"]["id"]
        body = client.get(f"/api/export/tasks/{task_id}", headers=auth_headers(t2)).json()
        assert body["code"] == 404


class TestLocalPoiSearch:
    """加点工作台检索（/api/pois）：数据来自 OTM 半径池 + 联网补池，无本地语料。"""

    def test_poi_search(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        body = client.get("/api/pois", params={"city": "北京", "keywords": "故宫"}, headers=auth_headers(token)).json()
        assert body["code"] == 200
        assert isinstance(body["data"]["items"], list)
        assert isinstance(body["data"]["coveredCities"], list)

    def test_poi_search_unknown_category_rejected(self, client):
        u = _uid()
        register(client, u)
        token = login(client, u)
        body = client.get(
            "/api/pois", params={"city": "北京", "category": "shopping"}, headers=auth_headers(token)
        ).json()
        assert body["code"] == 400
