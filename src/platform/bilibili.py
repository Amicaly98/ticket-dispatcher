"""Bilibili 平台 API 辅助端点。

提供前端号池管理所需的平台交互能力：扫码登录、活动查询、购票人列表、账号刷新等。
这些是「平台 API 调用」，不是「抢票执行逻辑」——抢票执行在 Driver 里，这里只做辅助查询。

通过 register_api_router() 注册到主 API，挂载在 /bilibili/ 前缀下。
Driver 作者可以参考此模块为其他平台实现类似的辅助端点。
"""
from __future__ import annotations
import logging
import os

from fastapi import APIRouter, HTTPException
import httpx

from ..api import register_api_router
from ..config import load_accounts

log = logging.getLogger("platform.bilibili")

router = APIRouter(prefix="/bilibili", tags=["bilibili"])

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


# ═══════════════════════════════════════════
#  扫码登录
# ═══════════════════════════════════════════

@router.post("/qr/gen")
async def qr_gen():
    """生成扫码登录二维码。返回 {qrcode_key, url}。"""
    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        r = await client.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        data = r.json().get("data", {})
        return {"qrcode_key": data.get("qrcode_key", ""), "url": data.get("url", "")}


@router.post("/qr/poll")
async def qr_poll(body: dict):
    """轮询扫码状态。入参 {qrcode_key}，返回 {status, cookie, uid}。"""
    qrcode_key = body.get("qrcode_key", "")
    if not qrcode_key:
        raise HTTPException(400, "缺少 qrcode_key")
    headers = {"User-Agent": _UA, "Referer": "https://passport.bilibili.com/login"}
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, headers=headers) as client:
        r = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key})
        data = r.json().get("data", {})
        code = data.get("code", -1)
        result = {"status": code, "message": data.get("message", "")}
        if code == 0:
            cookies = {}
            for name in ("SESSDATA", "bili_jct", "DedeUserID"):
                for c in r.cookies.items():
                    if c[0] == name:
                        cookies[name] = c[1]
            result["cookie"] = cookies
            result["uid"] = cookies.get("DedeUserID") or data.get("mid", "")
        return result


@router.post("/check/login")
async def check_login(body: dict):
    """检查登录状态。入参 {uid}，从 accounts 配置读 cookie 验证。"""
    uid = body.get("uid", 0)
    accounts = load_accounts()
    sess_data = ""
    for acc in accounts.values():
        if acc.uid == uid:
            sess_data = acc.cookie.get("sess_data", "") or acc.cookie.get("SESSDATA", "")
            break
    if not sess_data:
        return {"logged_in": False, "error": "账号无 SESSDATA，请扫码登录"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Cookie": f"SESSDATA={sess_data}"})
        data = r.json().get("data", {})
        if data.get("isLogin"):
            return {
                "logged_in": True, "uid": data.get("mid", 0),
                "username": data.get("uname", ""), "face": data.get("face", ""),
                "sessdata": sess_data,
            }
        return {"logged_in": False}


@router.post("/check/login/cookie")
async def check_login_cookie(body: dict):
    """用 cookie 直接检查登录状态。入参 {sess_data}。"""
    sess_data = body.get("sess_data", "")
    if not sess_data:
        return {"logged_in": False, "error": "无 SESSDATA"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"Cookie": f"SESSDATA={sess_data}", "User-Agent": _UA})
        data = r.json().get("data", {})
        if data.get("isLogin"):
            return {"logged_in": True, "uid": data.get("mid", 0), "username": data.get("uname", "")}
        return {"logged_in": False}


# ═══════════════════════════════════════════
#  活动 / 票务查询
# ═══════════════════════════════════════════

@router.post("/event/info")
async def event_info(body: dict):
    """查询活动详情（场次 + 票种）。入参 {project_id}。"""
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(400, "缺少 project_id")
    cookie_str = body.get("cookie", "")
    headers = {"User-Agent": _UA,
               "Referer": f"https://show.bilibili.com/platform/detail.html?id={project_id}"}
    if cookie_str:
        headers["Cookie"] = cookie_str
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://show.bilibili.com/api/ticket/project/get",
            params={"id": project_id}, headers=headers)
        data = r.json()
        if data.get("errno", -1) != 0:
            return {"error": data.get("msg", "查询失败"), "errno": data.get("errno", -1)}
        info = data.get("data", {})
        screens = []
        for s in info.get("screen_list", []):
            skus = []
            for sku in s.get("ticket_list", []):
                skus.append({
                    "sku_id": sku.get("id", 0), "desc": sku.get("desc", ""),
                    "price": sku.get("price", 0), "stock": sku.get("stock_text", ""),
                    "clickable": sku.get("clickable", False), "sale_start": sku.get("sale_start", ""),
                })
            screens.append({
                "screen_id": s.get("id", 0), "name": s.get("name", ""),
                "sale_start": s.get("sale_start", ""), "skus": skus,
            })
        return {
            "project_id": project_id, "name": info.get("name", ""),
            "venue_name": info.get("venue_name", ""), "city_name": info.get("city_name", ""),
            "screens": screens,
        }


@router.post("/event/stock")
async def event_stock(body: dict):
    """查询活动票务状态。入参 {project_id}。"""
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(400, "缺少 project_id")
    headers = {"User-Agent": _UA}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://show.bilibili.com/api/ticket/project/get",
            params={"id": project_id}, headers=headers)
        data = r.json()
        if data.get("errno", -1) != 0:
            return {"error": data.get("msg", "查询失败")}
        info = data.get("data", {})
        tickets = []
        for s in info.get("screen_list", []):
            for t in s.get("ticket_list", []):
                tickets.append({
                    "screen_id": s.get("id", 0), "sku_id": t.get("id", 0),
                    "clickable": t.get("clickable", False), "sale_start": t.get("sale_start", ""),
                    "stock_text": t.get("stock_text", ""), "desc": t.get("desc", ""),
                })
        return {
            "project_id": project_id, "name": info.get("name", ""),
            "hot_project": info.get("hotProject", False), "tickets": tickets,
        }


# ═══════════════════════════════════════════
#  购票人 / 账号刷新
# ═══════════════════════════════════════════

@router.post("/buyer/list")
async def buyer_list(body: dict):
    """查询账号的购票人列表。入参 {account_id}。"""
    account_id = body.get("account_id", "")
    accounts = load_accounts()
    acc = accounts.get(account_id)
    if not acc:
        return {"error": "账号不存在", "buyers": []}
    sess_data = acc.cookie.get("sess_data", "") or acc.cookie.get("SESSDATA", "")
    if not sess_data:
        return {"error": "账号 cookie 已失效或被清空，请重新登录", "buyers": []}
    headers = {"User-Agent": _UA, "Cookie": f"SESSDATA={sess_data}"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://show.bilibili.com/api/ticket/buyer/list", headers=headers)
        data = r.json()
        buyers = []
        for b in data.get("data", {}).get("list", []):
            buyers.append({
                "id": b.get("id", 0), "name": b.get("name", ""),
                "tel": b.get("tel", ""), "personal_id": b.get("personal_id", ""),
                "id_type": b.get("id_type", 0), "verify_status": b.get("verify_status", 0),
            })
        return {"buyers": buyers, "count": len(buyers)}


@router.post("/account/refresh")
async def account_refresh(body: dict):
    """刷新账号的购票人 + 大会员状态并写回 accounts.yaml。入参 {account_id}。"""
    from ..api import _update_account_fields, _bus
    account_id = body.get("account_id", "")
    acc = load_accounts().get(account_id)
    if not acc:
        raise HTTPException(404, f"账号 {account_id} 不存在")
    sess_data = acc.cookie.get("sess_data", "") or acc.cookie.get("SESSDATA", "")
    if not sess_data:
        return {"error": "账号无 SESSDATA，请重新登录", "logged_in": False}
    headers = {"User-Agent": _UA, "Cookie": f"SESSDATA={sess_data}"}
    async with httpx.AsyncClient(timeout=10) as client:
        nav = await client.get("https://api.bilibili.com/x/web-interface/nav", headers=headers)
        ndata = nav.json().get("data", {})
        if not ndata.get("isLogin"):
            return {"error": "cookie 已失效，请重新登录", "logged_in": False}
        vip_status = ndata.get("vipStatus", 0)
        vip_type = ndata.get("vipType", 0)
        vip_due_date = ndata.get("vipDueDate", 0)
        username = ndata.get("uname", "") or acc.username
        r = await client.get("https://show.bilibili.com/api/ticket/buyer/list", headers=headers)
        buyers = []
        for b in r.json().get("data", {}).get("list", []):
            buyers.append({
                "id": b.get("id", 0), "name": b.get("name", ""),
                "tel": b.get("tel", ""), "personal_id": b.get("personal_id", ""),
                "id_type": b.get("id_type", 0),
            })
    _update_account_fields(account_id, {
        "username": username, "buyers": buyers,
        "vip_status": vip_status, "vip_type": vip_type, "vip_due_date": vip_due_date,
    })
    if _bus:
        _bus.publish({"type": "account_refreshed", "account_id": account_id})
    return {
        "status": "ok", "logged_in": True, "account_id": account_id,
        "buyers": len(buyers), "vip_status": vip_status, "vip_type": vip_type,
        "vip_due_date": vip_due_date,
    }


# 自动注册
register_api_router(router)
