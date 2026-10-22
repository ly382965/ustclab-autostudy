"""
中国科学技术大学实验室安全准入考试 (sysaq.ustc.edu.cn) 自动学习助手
支持 USTC CAS 统一身份认证，断点续刷，定时心跳保活
"""

import os
import sys
import time
import json
import base64
import hashlib
import asyncio
import argparse
import pathlib
from typing import Dict, List, Optional, Any

# 确保学校内网域名不走系统/用户代理
os.environ['NO_PROXY'] = 'ustc.edu.cn,localhost,127.0.0.1'
os.environ['no_proxy'] = 'ustc.edu.cn,localhost,127.0.0.1'

import httpx
from pyustc import CASClient

# 常量配置 (逆向自 lab-study-front / chingo.js)
APP_SECRET = "c640ca392cd45fb3a55b00a63a86c618"
APP_KEY = "8fceb735082b5a529312040b58ea780b"
BASE_URL = "https://sysaq.ustc.edu.cn/cg-exam-server"
CAS_SERVICE = "http://sysaq.ustc.edu.cn/admin/caslogin"
SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
TOKEN_FILE = SCRIPT_DIR / "token.txt"
CONFIG_FILE = SCRIPT_DIR / "config.json"

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def get_edge_path() -> str:
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    raise FileNotFoundError("未找到系统中的 Microsoft Edge 浏览器 (msedge.exe)")


def load_file_config() -> Dict[str, Any]:
    """从 config.json 读取外部配置（如果存在）"""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception as e:
            print(f"[Warn] 读取 config.json 失败: {e}")
    return {}


def make_sign(timestamp: int, path: str, data: Optional[Dict[str, Any]] = None) -> str:
    """生成 lab-study-front 请求签名"""
    t = APP_SECRET + path
    if isinstance(data, dict):
        for k in sorted(data.keys()):
            v = data[k]
            if not isinstance(v, (dict, list)) and v not in ('', None):
                t += str(k) + str(v)
    t += str(timestamp) + " " + APP_SECRET
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def is_jwt_valid(token: str) -> bool:
    """检查 JWT token 是否存在且未过期"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        exp = data.get("exp", 0)
        # 预留 10 分钟缓冲
        return exp > (time.time() + 600)
    except Exception:
        return False


class AuthManager:
    """认证管理器，负责 CAS 登录并获取持久化 JWT token"""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    async def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text("utf-8").strip()
            if is_jwt_valid(token):
                return token

        print("[Auth] Token 已过期或不存在，正在通过 USTC CAS 统一身份认证登录...")
        token = await self._login_and_extract_token()
        TOKEN_FILE.write_text(token, "utf-8")
        print("[Auth] 登录成功，Token 已保存至本地缓存。")
        return token

    async def _login_and_extract_token(self) -> str:
        from playwright.async_api import async_playwright

        # 1. 通过 pyustc 登录 CAS 并换取 ST ticket
        async with CASClient.login_by_pwd(self.username, self.password) as cas:
            ticket = await cas.get_ticket(CAS_SERVICE)

        # 2. 交换 ticket 获取带 sso_token 的重定向链接
        async with httpx.AsyncClient(verify=False, follow_redirects=False) as client:
            r1 = await client.get(CAS_SERVICE, params={"ticket": ticket})
            if "location" not in r1.headers:
                raise RuntimeError(f"CAS 回调未重定向: {r1.status_code}")
            r2 = await client.get(r1.headers["location"])
            target_url = r2.headers.get("location")
            if not target_url or "sso_token" not in target_url:
                raise RuntimeError(f"未能获取 sso_token 重定向链接: {r2.status_code}")

        # 3. 使用无头 Edge 访问并在前端完成 token 置换
        edge_exe = get_edge_path()
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=edge_exe, headless=True)
            page = await browser.new_page()
            try:
                await page.goto(target_url)
                try:
                    await page.wait_for_url("**/lab-study-front/**", timeout=15000)
                except Exception:
                    await page.wait_for_timeout(3000)
                token = await page.evaluate("() => localStorage.getItem('dataSix')")
                if not token:
                    raise RuntimeError("未能从浏览器 localStorage 获取 dataSix 认证信息")
                return token
            finally:
                await browser.close()


class StudyBot:
    """刷课机器人"""

    def __init__(self, auth: AuthManager, exam_id: int = 45):
        self.auth = auth
        self.exam_id = exam_id
        self.token: Optional[str] = None
        self.client: Optional[httpx.AsyncClient] = None

    async def init(self):
        self.token = await self.auth.get_token()
        self.client = httpx.AsyncClient(verify=False, timeout=20.0)

    async def close(self):
        if self.client:
            await self.client.aclose()

    def _headers(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        ts = int(time.time() * 1000)
        sign = make_sign(ts, path, data)
        return {
            "app-key": APP_KEY,
            "timestamp": str(ts),
            "sign": sign,
            "cgAuthorization": self.token or "",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    async def fetch_all_contents(self) -> List[Dict[str, Any]]:
        """获取当前考试任务下的所有学习内容列表"""
        page = 0
        all_items = []
        while True:
            path = "/api/page_study_content"
            params = {
                "page": page,
                "size": 50,
                "examId": self.exam_id,
                "nocache": int(time.time() * 1000),
            }
            headers = self._headers(path, params)
            resp = await self.client.get(f"{BASE_URL}{path}", params=params, headers=headers)
            res_json = resp.json()
            if res_json.get("code") == 401:
                # Token 失效，刷新后重试
                print("[Warn] Token 已失效，正在重新登录...")
                self.token = await self.auth.get_token(force_refresh=True)
                continue

            data = res_json.get("data", {})
            content = data.get("content", [])
            if not content:
                break
            all_items.extend(content)
            total_elements = data.get("totalElements", 0)
            if len(all_items) >= total_elements:
                break
            page += 1

        return all_items

    async def start_study(self, table_code: int, content_id: int) -> Optional[str]:
        """开启单门课程的学习 session"""
        path = f"/api/study/{table_code}/{content_id}"
        ts = int(time.time() * 1000)
        data = {"beginTime": ts, "examId": str(self.exam_id)}
        headers = self._headers(path, data)
        resp = await self.client.post(f"{BASE_URL}{path}", data=data, headers=headers)
        res_json = resp.json()
        if res_json.get("code") == 200:
            return res_json.get("data", {}).get("studyId")
        print(f"[Error] 启动学习失败: {res_json}")
        return None

    async def upload_heartbeat(self, study_id: str) -> bool:
        """上传单次学习心跳 (15秒)"""
        path = f"/api/study_upload/{study_id}"
        ts = int(time.time() * 1000)
        data = {"uploadTime": ts}
        headers = self._headers(path, data)
        try:
            resp = await self.client.post(f"{BASE_URL}{path}", data=data, headers=headers)
            res_json = resp.json()
            return res_json.get("code") == 200
        except Exception as e:
            print(f"[Warn] 心跳上传请求异常: {e}")
            return False

    async def show_status(self):
        """打印当前考试学习进度总览"""
        items = await self.fetch_all_contents()
        must_items = [it for it in items if it.get("mustStudy") == 1]
        opt_items = [it for it in items if it.get("mustStudy") != 1]

        def calc_stats(group):
            req = sum(it.get("requireStudyLength", 0) for it in group)
            done = sum(min(it.get("studyLength", 0), it.get("requireStudyLength", 0)) for it in group)
            finished_cnt = sum(1 for it in group if it.get("finish") or it.get("studyLength", 0) >= it.get("requireStudyLength", 0))
            return req, done, finished_cnt, len(group)

        m_req, m_done, m_fin, m_total = calc_stats(must_items)
        o_req, o_done, o_fin, o_total = calc_stats(opt_items)

        def fmt_time(seconds):
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return f"{h:02d}:{m:02d}:{s:02d}"

        print("=" * 60)
        print(f"【实验室安全准入考试 (Task {self.exam_id}) 学习进度总览】")
        print(f"必修课程: {m_fin}/{m_total} 篇已完成 | 时长: {fmt_time(m_done)} / {fmt_time(m_req)} ({m_done/max(m_req, 1)*100:.1f}%)")
        print(f"选修课程: {o_fin}/{o_total} 篇已完成 | 时长: {fmt_time(o_done)} / {fmt_time(o_req)} ({o_done/max(o_req, 1)*100:.1f}%)")
        print(f"总计剩余需学习: 必修 {fmt_time(max(0, m_req - m_done))} | 选修 {fmt_time(max(0, o_req - o_done))}")
        print("=" * 60)

    async def run_study(self, mode: str = "must"):
        """自动化刷课循环"""
        items = await self.fetch_all_contents()
        if mode == "must":
            targets = [it for it in items if it.get("mustStudy") == 1]
        elif mode == "all":
            targets = items
        elif mode == "opt":
            targets = [it for it in items if it.get("mustStudy") != 1]
        else:
            targets = items

        # 过滤未完成课程
        pending = [
            it for it in targets
            if not it.get("finish") and it.get("studyLength", 0) < it.get("requireStudyLength", 0)
        ]

        if not pending:
            print("\n🎉 所有指定课程已达到要求学习时长，无需继续刷课！")
            return

        total_pending_seconds = sum(
            it.get("requireStudyLength", 0) - it.get("studyLength", 0)
            for it in pending
        )

        print(f"\n🚀 开始自动化刷课，当前待学课程共 {len(pending)} 门，需累计学习 {total_pending_seconds // 60} 分钟")
        print("说明：心跳周期为 15 秒（与官方系统一致），请耐心挂机运行，随时可按 Ctrl+C 安全停止。\n")

        for idx, item in enumerate(pending, 1):
            title = item.get("title")
            cid = item.get("id")
            table_code = item.get("tableCode")
            req_len = item.get("requireStudyLength", 0)
            cur_len = item.get("studyLength", 0)
            tag = "必学" if item.get("mustStudy") == 1 else "选学"

            need_seconds = max(0, req_len - cur_len)
            print(f"[{idx}/{len(pending)}] [{tag}] 正在学习: 《{title}》")
            print(f"    要求时长: {req_len}s | 已学: {cur_len}s | 本次需刷: {need_seconds}s")

            study_id = await self.start_study(table_code, cid)
            if not study_id:
                print(f"    ❌ 无法启动课程 session，跳过。")
                continue

            session_studied = 0
            while session_studied < need_seconds:
                # 每次心跳间隔 15 秒 (若最后剩余不足 15 秒则取剩余秒数)
                wait_step = min(15, need_seconds - session_studied)
                await asyncio.sleep(wait_step)
                session_studied += wait_step
                cur_total = cur_len + session_studied

                ok = await self.upload_heartbeat(study_id)
                pct = cur_total / max(req_len, 1) * 100
                status_char = "✓" if ok else "!"
                print(f"    [{status_char}] 心跳上传 | 进度: {cur_total}s / {req_len}s ({pct:5.1f}%) | 本课剩余: {max(0, need_seconds - session_studied)}s")

            print(f"    ✨ 《{title}》 学习完成！\n")
            # 课程切换间歇
            await asyncio.sleep(2)

        print("==================================================")
        print("🎉 选定任务所有课程学习完成！系统将在5分钟内同步最终进度。")
        print("==================================================")


def parse_args():
    file_cfg = load_file_config()

    parser = argparse.ArgumentParser(description="中国科学技术大学实验室安全准入考试自动刷课脚本")
    parser.add_argument(
        "--usr",
        default=os.getenv("USTC_CAS_USR") or file_cfg.get("username", ""),
        help="统一身份认证学工号 (推荐使用环境变量 USTC_CAS_USR 或 config.json)",
    )
    parser.add_argument(
        "--pwd",
        default=os.getenv("USTC_CAS_PWD") or file_cfg.get("password", ""),
        help="统一身份认证密码 (推荐使用环境变量 USTC_CAS_PWD 或 config.json)",
    )
    parser.add_argument(
        "--exam-id",
        type=int,
        default=int(os.getenv("USTC_EXAM_ID") or file_cfg.get("exam_id", 45)),
        help="考试任务 ID (默认 45)",
    )
    parser.add_argument(
        "--mode",
        choices=["must", "all", "opt"],
        default=file_cfg.get("mode", "must"),
        help="刷课模式: must (只刷必修, 默认), all (必修+选修), opt (只刷选修)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="仅查看当前学习进度总览，不执行刷课",
    )
    return parser.parse_args()


async def async_main():
    args = parse_args()

    if not args.usr or not args.pwd:
        print("=" * 60)
        print("[Error] 未检测到统一身份认证账号或密码！")
        print("为了账号安全，推荐通过以下方式之一提供凭据（优先推荐环境变量）：")
        print()
        print("  方式 1 (强烈推荐 - 环境变量):")
        print("    Windows cmd:        set USTC_CAS_USR=SAxxxxxxx && set USTC_CAS_PWD=你的密码")
        print("    Windows PowerShell: $env:USTC_CAS_USR='SAxxxxxxx'; $env:USTC_CAS_PWD='你的密码'")
        print("    Linux/macOS bash:   export USTC_CAS_USR=SAxxxxxxx USTC_CAS_PWD=你的密码")
        print()
        print("  方式 2 (本地配置文件):")
        print("    复制 config.example.json 为 config.json，并在其中填入账号密码（config.json 已被 .gitignore 忽略）")
        print()
        print("  方式 3 (命令行参数):")
        print("    python study_bot.py --usr SAxxxxxxx --pwd 你的密码")
        print("=" * 60)
        sys.exit(1)

    auth = AuthManager(args.usr, args.pwd)
    bot = StudyBot(auth, exam_id=args.exam_id)

    try:
        await bot.init()
        await bot.show_status()

        if args.status:
            return

        await bot.run_study(mode=args.mode)
    except KeyboardInterrupt:
        print("\n[Stop] 用户手动中断运行，已完成的心跳数据已保存在服务器端。下次运行可自动断点续刷。")
    finally:
        await bot.close()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
