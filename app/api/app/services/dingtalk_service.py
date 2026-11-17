"""钉钉免登 / 用户信息（调试用）。

未配置应用凭据（dingtalk_app_key / dingtalk_app_secret）时优雅降级：
不报错，仅回显前端拿到的 authCode，便于在真机上先验证免登链路是否打通。
配置后走标准 oapi 流程：gettoken → topapi/v2/user/getuserinfo → topapi/v2/user/get。
"""

from __future__ import annotations

import time

from app.config import settings

_OAPI = "https://oapi.dingtalk.com"
_token_cache: dict = {"token": None, "exp": 0.0}


def is_configured() -> bool:
    return bool(settings.dingtalk_app_key and settings.dingtalk_app_secret)


async def _get_access_token() -> str:
    import httpx

    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] > now + 60:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"{_OAPI}/gettoken",
            params={
                "appkey": settings.dingtalk_app_key,
                "appsecret": settings.dingtalk_app_secret,
            },
        )
        data = r.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"gettoken failed: {data}")
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = now + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def _agent_id() -> int | str:
    try:
        return int(settings.dingtalk_agent_id)
    except (TypeError, ValueError):
        return settings.dingtalk_agent_id


# 推送被开关拦截时的统一返回（与真发送失败 error 区分：skipped=True）。
_PUSH_SKIP_REASON = "目标用户未开启钉钉通知，已跳过"


def _skipped_result() -> dict:
    return {
        "ok": False, "skipped": True, "reason": _PUSH_SKIP_REASON,
        "error": _PUSH_SKIP_REASON, "forbidden": [], "invalidUsers": [], "taskId": None,
    }


def _push_allowed(dd_userid: str) -> bool:
    """重要：发送前判断该钉钉用户是否允许推送（employee.push_enabled）。

    用 dd_userid 反查花名册；查不到对应员工 → 按 False（不发，安全侧）。
    底层纯函数无 db 参数，故自开短事务查询，查完即关。任何异常按 False（不误发）。
    """
    uid = (dd_userid or "").strip()
    if not uid:
        return False
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import Employee

        db = SessionLocal()
        try:
            row = db.scalar(
                select(Employee.push_enabled).where(Employee.dd_user_id == uid)
            )
            return bool(row)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — 查询异常时不发（安全侧），不打断调用方
        return False


async def send_work_message(dd_userid: str, text: str) -> dict:
    """发钉钉工作通知(text)给指定 userid。需配置 app_key/secret + agent_id。

    重要：发送前判断 push_enabled，未开启则跳过不发（返回 skipped 结构，不抛）。

    注意：asyncsend_v2 是异步接口，errcode=0 仅表示任务受理；
    目标无效/不在应用可见范围等失败要用 get_send_result(task_id) 查询。
    """
    import logging

    logger = logging.getLogger("fdc.dingtalk")
    # 重要：开关拦截——未开启钉钉通知则不发，返回 accept-like 但带 skipped 标记（task_id=None，
    # errcode=0 表示"已处理但跳过发送"，不抛错以免打断批量推送）。
    if not _push_allowed(dd_userid):
        logger.info("push skipped (push_enabled=false): %s", dd_userid)
        return {"errcode": 0, "skipped": True, "reason": _PUSH_SKIP_REASON, "task_id": None}
    if not is_configured():
        raise RuntimeError("钉钉应用凭据未配置（dingtalk_app_key/secret）")
    if not settings.dingtalk_agent_id:
        raise RuntimeError("缺少 dingtalk_agent_id")

    import httpx

    token = await _get_access_token()
    payload = {
        "agent_id": _agent_id(),
        "userid_list": dd_userid,
        "msg": {"msgtype": "text", "text": {"content": text}},
    }
    logger.info("dingtalk asyncsend_v2 → userid_list=%s agent_id=%s", dd_userid, payload["agent_id"])
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{_OAPI}/topapi/message/corpconversation/asyncsend_v2",
            params={"access_token": token},
            json=payload,
        )
        data = r.json()
    logger.info("dingtalk asyncsend_v2 ← %s", data)
    if data.get("errcode") != 0:
        raise RuntimeError(f"asyncsend failed: {data}")
    return data


# 钉钉工作通知投递失败码 → 人话（forbidden_list[].code）
_FORBIDDEN_CODE_REASON = {
    "143106": "员工不在该应用可见范围（或无使用权限）",
    "143107": "员工已离职或被禁用",
}


def interpret_send_result(send_result: dict | None) -> dict:
    """解析 getsendresult：返回 {ok, invalidUsers, forbidden:[{userid,code,reason}], error}。
    ok=False 表示有用户实际未送达（不在可见范围/无效/被禁止）——应作为错误暴露给前台。"""
    sr = (send_result or {}).get("send_result") or {}
    invalid = sr.get("invalid_user_id_list") or []
    forbidden_ids = sr.get("forbidden_user_id_list") or []
    forbidden_detail = sr.get("forbidden_list") or []
    code_by_user = {f.get("userid"): str(f.get("code")) for f in forbidden_detail if f.get("userid")}
    forbidden = [
        {
            "userid": uid,
            "code": code_by_user.get(uid, ""),
            "reason": _FORBIDDEN_CODE_REASON.get(code_by_user.get(uid, ""), "被禁止接收"),
        }
        for uid in forbidden_ids
    ]
    ok = not invalid and not forbidden
    parts = []
    if forbidden:
        parts.append(
            "；".join(f"{x['userid']} 被禁止接收（{x['code'] or '无码'}：{x['reason']}）" for x in forbidden)
        )
    if invalid:
        parts.append(f"无效用户（不在通讯录/可见范围）：{invalid}")
    return {
        "ok": ok,
        "invalidUsers": invalid,
        "forbidden": forbidden,
        "error": "；".join(parts) if parts else "",
    }


async def get_send_result(task_id: int | str) -> dict:
    """查询工作通知异步发送结果：invalid_user_id_list / forbidden_user_id_list 等。"""
    import logging

    import httpx

    logger = logging.getLogger("fdc.dingtalk")
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(
            f"{_OAPI}/topapi/message/corpconversation/getsendresult",
            params={"access_token": token},
            json={"agent_id": _agent_id(), "task_id": task_id},
        )
        data = r.json()
    logger.info("dingtalk getsendresult(task_id=%s) ← %s", task_id, data)
    return data


def send_work_message_sync(dd_userid: str, text: str) -> dict:
    """同步发钉钉工作通知并查投递结果（供同步代码/调度线程调用）。
    返回 {ok, taskId, error, forbidden, invalidUsers, raw}；ok=False 表示实际未送达。
    skipped=True 表示因用户未开启钉钉通知被跳过（与真发送失败 error 区分）。
    在 FastAPI 同步端点(线程池) / apscheduler 线程内调用——无运行中的事件循环，可 asyncio.run。"""
    import asyncio

    # 重要：开关拦截——未开启则不发，返回 skipped 结构（ok=False, skipped=True）。
    if not _push_allowed(dd_userid):
        return _skipped_result()

    async def _flow() -> dict:
        accept = await send_work_message(dd_userid, text)  # errcode!=0 抛错
        task_id = accept.get("task_id")
        send_result = None
        if task_id:
            await asyncio.sleep(1.5)
            try:
                send_result = await get_send_result(task_id)
            except Exception:  # noqa: BLE001
                send_result = None
        interp = interpret_send_result(send_result)
        return {**interp, "taskId": task_id, "raw": send_result}

    try:
        return asyncio.run(_flow())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"发送失败：{e}", "forbidden": [], "invalidUsers": [], "taskId": None}


async def login_by_code(code: str) -> dict:
    """用免登 authCode 换取用户信息。无凭据时仅回显 code。"""
    if not is_configured():
        return {
            "configured": False,
            "authCode": code,
            "message": "钉钉应用凭据未配置（dingtalk_app_key/secret），仅回显 authCode；"
            "配置后将返回真实用户信息。",
        }

    import httpx

    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=10) as c:
        ui = (
            await c.post(
                f"{_OAPI}/topapi/v2/user/getuserinfo",
                params={"access_token": token},
                json={"code": code},
            )
        ).json()
        if ui.get("errcode") != 0:
            raise RuntimeError(f"getuserinfo failed: {ui}")
        userid = ui["result"]["userid"]
        detail = (
            await c.post(
                f"{_OAPI}/topapi/v2/user/get",
                params={"access_token": token},
                json={"userid": userid},
            )
        ).json()
    ok = detail.get("errcode") == 0
    res = detail.get("result", {}) if ok else {}
    out = {
        "configured": True,
        "authCode": code,
        "userid": userid,
        "name": res.get("name"),
        "mobile": res.get("mobile"),
        "title": res.get("title"),
        "deptIdList": res.get("dept_id_list"),
        "raw": detail,
    }
    if not ok:
        # user/get 失败通常是应用缺『通讯录个人信息读权限』；把错误透出便于排查
        out["userGetError"] = {
            "errcode": detail.get("errcode"),
            "errmsg": detail.get("errmsg"),
        }
        out["hint"] = (
            "拿到 userid 但查不到姓名：该应用缺少『通讯录个人信息读权限/成员信息读权限』。"
            "请到钉钉开放平台→应用→权限管理添加后重试。"
        )
    return out
