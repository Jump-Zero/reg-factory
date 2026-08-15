# -*- coding: utf-8 -*-
"""
run_full_flow.py — 端到端全流程编排（含邮箱注册）

把项目原有的两个阶段串成一条龙，方便"跑一遍看看"：

  Stage A  邮箱注册   outlook_reg_loop.py        -> 产出新 outlook 号写进 emails.txt
  Stage B  平台注册   register_three_platforms   -> 用该号去 claude/chatgpt/grok 注册

Stage A 本身是个常驻循环，这里把它当子进程拉起、盯着 emails.txt，**一旦冒出
一个新的可用号就立刻杀掉循环**进入 Stage B，所以是"注册到一个邮箱就往下走"。

前置：BitBrowser(54345) 在线、Clash Verge(控制器 9097 / 混合端口 7897) 在线。
默认自动注入 HTTP(S)_PROXY 与 CLASH_API/SECRET/GROUP，让邮箱注册能换节点绕 MS 风控。

用法：
  python run_full_flow.py                          # 注册1个邮箱 -> 在 claude 上注册
  python run_full_flow.py --platforms claude chatgpt
  python run_full_flow.py --rounds 12 --concurrency 3 --platforms claude chatgpt github
  python run_full_flow.py --platforms chatgpt --rounds 10   # 循环注册 10 个号
  python run_full_flow.py --platforms chatgpt --rounds 0    # 无限循环（Ctrl+C 停）
  python run_full_flow.py --skip-email --email a@outlook.com --password xxx   # 跳过邮箱注册
  python run_full_flow.py --email-attempts 20 --email-timeout 180
  python run_full_flow.py --dry-run                # 只打印将要执行的命令
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import subprocess
import sys
import time
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.environ.get("REG_FACTORY_DATA_DIR", "").strip() or ROOT
EMAILS_FILE = os.path.join(DATA_ROOT, "emails.txt")

# 导入 config 以触发 .env 加载（CLASH_SECRET 等环境变量来自 .env / 真实环境）。
try:
    import config  # noqa: F401
except Exception:
    pass

# 默认基建端点（密钥走环境变量，端点可被环境变量覆盖）。
CLASH_API_DEFAULT = os.environ.get("CLASH_API", "http://127.0.0.1:9097")
CLASH_SECRET_DEFAULT = os.environ.get("CLASH_SECRET", "")
from common import proxy_switch
from common.concurrency import build_worker_plan

PROXY_DEFAULT = proxy_switch.effective_proxy_url()


def log(msg, level="INFO"):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


def redact_command(cmd):
    """Render a child command without leaking mailbox credentials or tokens."""
    secret_flags = {"--password", "--token", "--refresh-token", "--c2a-key", "--clash-secret"}
    rendered = []
    hide_next = False
    for part in cmd:
        text = str(part)
        if hide_next:
            rendered.append("***")
            hide_next = False
            continue
        rendered.append(text)
        hide_next = text in secret_flags
    return " ".join(rendered)


# ---------------------------------------------------------------- emails.txt
def read_fresh_emails():
    """返回 emails.txt 里全部 (email, password, token, client_id) 条目（含已 reserve 的，纯快照用于 diff）。
    token/client_id 由 outlook_reg_loop 注册成功后抽 Graph 写入；缺列回退空串。"""
    out = []
    if not os.path.isfile(EMAILS_FILE):
        return out
    with open(EMAILS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("----")
            email = parts[0].strip()
            password = parts[1].strip() if len(parts) > 1 else ""
            token = parts[2].strip() if len(parts) > 2 else ""
            client_id = parts[3].strip() if len(parts) > 3 else ""
            out.append((email, password, token, client_id))
    return out


# ---------------------------------------------------------------- env
def build_child_env(args):
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if args.proxy:
        configured_mode = proxy_switch.proxy_mode(env)
        configured_proxy = proxy_switch.effective_proxy_url(env)
        if configured_mode == "residential" or args.proxy != configured_proxy:
            env["PROXY_MODE"] = "residential"
            env["REG_FACTORY_PROXY"] = args.proxy
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = args.proxy
        env["http_proxy"] = env["https_proxy"] = args.proxy
        # 关键：localhost API(BitBrowser 54345 / Clash 控制器 9097) 必须直连，
        # 否则 urllib 把它们也塞进 7897 代理 -> 502 Bad Gateway。
        no_proxy = "127.0.0.1,localhost,::1"
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
    # 让 outlook_reg_loop 的 _clash_verge 能连控制器换节点
    env["CLASH_API"] = args.clash_api
    env["CLASH_SECRET"] = args.clash_secret
    env["CLASH_GROUP"] = args.clash_group
    task_tuning = {
        "CLAUDE_RESIDENTIAL_PROFILE_RETRIES": getattr(args, "claude_profile_retries", None),
        "CLAUDE_HCAPTCHA_SOLVE_RETRIES": getattr(args, "claude_hcaptcha_retries", None),
        "CODEX_PHONE_SKIP_ATTEMPTS": getattr(args, "codex_phone_skip", None),
        "CODEX_ADDPHONE_ATTEMPTS": getattr(args, "codex_phone_attempts", None),
        "CODEX_SMS_TIMEOUT": getattr(args, "codex_sms_timeout", None),
        "SMS_GETPHONE_RETRIES": getattr(args, "sms_get_phone_retries", None),
        "CUSTOM_SMS_POOL_FILE": getattr(args, "custom_sms_pool_file", None),
        "CUSTOM_SMS_ALLOWED_HOSTS": getattr(args, "custom_sms_allowed_hosts", None),
    }
    for key, value in task_tuning.items():
        if value not in (None, ""):
            env[key] = str(value)
    return env


# ---------------------------------------------------------------- Stage A
def stage_emails(args, env, target_count=1, concurrency=1):
    """拉起一个 Outlook 生产进程，返回最多 target_count 个新邮箱。"""
    target_count = max(1, int(target_count or 1))
    concurrency = max(1, min(target_count, int(concurrency or 1)))
    before = {e for e, _p, _t, _c in read_fresh_emails()}
    log(f"Stage A 邮箱注册启动；emails.txt 现有 {len(before)} 个号", "A")

    cmd = [
        sys.executable, "outlook_reg_loop.py",
        "--count", str(max(args.email_attempts, target_count)),
        "--concurrency", str(concurrency),
        "--timeout", str(args.email_timeout),
        "--max-press", str(args.max_press),
        "--sleep", "3",
    ]
    if args.email_confirm_before_register:
        cmd.append("--confirm-before-register")
    log(f"Stage A cmd: {' '.join(cmd)}", "A")
    if args.dry_run:
        return [
            (f"dry-run-{index}@outlook.com", "DryRunPass1!", "", "")
            for index in range(1, target_count + 1)
        ]

    outlook_env = proxy_switch.platform_environment(env, "outlook")
    log(f"Stage A proxy mode: {proxy_switch.proxy_mode(outlook_env)}", "A")
    proc = subprocess.Popen(
        cmd, cwd=DATA_ROOT, env=outlook_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    new_emails = []
    try:
        deadline = time.time() + args.email_total_timeout
        # 边读子进程日志边透传，同时每读几行 poll 一次 emails.txt
        assert proc.stdout is not None
        last_check = 0.0
        while True:
            if proc.poll() is not None:
                log("Stage A 子进程已退出", "A")
                break
            line = proc.stdout.readline()
            if line:
                print(f"  [outlook] {line}", end="", flush=True)
            now = time.time()
            if now - last_check >= 2:
                last_check = now
                cur = read_fresh_emails()
                fresh = [t for t in cur if t[0] not in before]
                if len(fresh) >= target_count:
                    new_emails = fresh[:target_count]
                    log(f"Stage A 已收集 {len(new_emails)}/{target_count} 个新邮箱", "A")
                    break
            if now > deadline:
                log(f"Stage A 总超时 {args.email_total_timeout}s 仍无新号", "A")
                break
            if not line:
                time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    if not new_emails:
        fresh = [t for t in read_fresh_emails() if t[0] not in before]
        new_emails = fresh[:target_count]
    return new_emails


def stage_email(args, env):
    """兼容原来的单邮箱 Stage A 调用。"""
    accounts = stage_emails(args, env, target_count=1, concurrency=1)
    return accounts[0] if accounts else None


# ---------------------------------------------------------------- Stage B
def stage_platforms(args, env, email, password, token="", client_id=""):
    log(f"Stage B 平台注册：{email}  platforms={','.join(args.platforms)}", "B")
    # token 由 Stage A 注册时抽 Graph 写入 emails.txt；有真 token 走 Graph API 直收码(免浏览器)，
    # 没有(抽取失败回退 fresh/空)则下游退化到浏览器/broker 取码。
    cmd = [
        sys.executable, "register_three_platforms.py",
        "--email", email,
        "--password", password or "",
        "--token", (token or "fresh"),
        "--platforms", *args.platforms,
        "--node", args.node,
        "--chatgpt-country", args.chatgpt_country,
        "--timeout", str(args.platform_timeout),
        "--broker", args.broker,
        "--platform-retries", str(max(0, getattr(args, "platform_retries", 0))),
        "--grok-timeout", str(max(1, getattr(args, "grok_timeout", 40))),
        "--claude-profile-retries", str(max(1, getattr(args, "claude_profile_retries", 3))),
        "--claude-hcaptcha-retries", str(max(1, getattr(args, "claude_hcaptcha_retries", 2))),
        "--claude-challenge-wait", str(max(0, getattr(args, "claude_challenge_wait", 45))),
        "--claude-challenge-node-retries", str(max(0, getattr(args, "claude_challenge_node_retries", 3))),
        "--claude-captcha-manual-timeout", str(max(0, getattr(args, "claude_captcha_manual_timeout", 0))),
        "--codex-sms-provider", getattr(args, "codex_sms_provider", "auto"),
        "--codex-timeout", str(max(1, getattr(args, "codex_timeout", 120))),
        "--codex-phone-skip", str(max(0, getattr(args, "codex_phone_skip", 0))),
        "--codex-phone-attempts", str(max(1, getattr(args, "codex_phone_attempts", 2))),
        "--codex-sms-timeout", str(max(1, getattr(args, "codex_sms_timeout", 150))),
        "--sms-get-phone-retries", str(max(1, getattr(args, "sms_get_phone_retries", 4))),
        "--grok-mailbox-attempts", str(max(1, getattr(args, "grok_mailbox_attempts", 6))),
    ]
    if not getattr(args, "sequential_platforms", False):
        cmd.append("--parallel")
    if client_id and client_id != "fresh":
        cmd += ["--client-id", client_id]
    if args.keep_on_fail:
        cmd.append("--keep-on-fail")
    if args.import_c2a:
        cmd.append("--import-c2a")  # 透传给 register_three_platforms -> register_chatgpt
    if getattr(args, "plus_subscription", False):
        cmd.append("--plus-subscription")
    if args.codex:
        cmd.append("--codex")  # 透传给 register_three_platforms -> register_chatgpt
        if args.codex_group:
            cmd += ["--codex-group", args.codex_group]
        if args.codex_manual_phone:
            cmd.append("--codex-manual-phone")
    if args.grok_sub2api:
        cmd.append("--grok-sub2api")
        if args.grok_sub2api_group:
            cmd += ["--grok-sub2api-group", args.grok_sub2api_group]
    if getattr(args, "kiro_account_password", ""):
        cmd += ["--kiro-account-password", args.kiro_account_password]
    if getattr(args, "kiro_full_name", ""):
        cmd += ["--kiro-full-name", args.kiro_full_name]
    if getattr(args, "custom_sms_pool_file", ""):
        cmd += ["--custom-sms-pool-file", args.custom_sms_pool_file]
    if getattr(args, "custom_sms_allowed_hosts", ""):
        cmd += ["--custom-sms-allowed-hosts", args.custom_sms_allowed_hosts]
    if getattr(args, "skip_claude_validation", False):
        cmd.append("--no-claude-auto-validate")
    log(f"Stage B cmd: {redact_command(cmd)}", "B")
    if args.dry_run:
        return 0
    proc = subprocess.Popen(cmd, cwd=DATA_ROOT, env=env)
    return proc.wait()


def run_wave(args, env, target_count):
    """并发产出一批邮箱，再并发运行每个邮箱的平台注册管线。"""
    plan = build_worker_plan("full-flow", target_count, args.concurrency, env)
    plan.log()
    wave_size = plan.effective_concurrency
    accounts = stage_emails(args, env, target_count=wave_size, concurrency=wave_size)
    if not accounts:
        return [(1, "")]

    def run_account(index, account):
        email, password, token, client_id = account
        account_env = plan.worker(index).merged_environment(env)
        started = time.time()
        rc = stage_platforms(
            args,
            account_env,
            email,
            password or args.password,
            token,
            client_id,
        )
        log(
            f"管线结束 email={email} exit={rc} 用时={time.time() - started:.0f}s",
            "OK" if rc == 0 else "WARN",
        )
        return rc, email

    results = []
    with ThreadPoolExecutor(max_workers=plan.effective_concurrency) as executor:
        futures = {
            executor.submit(run_account, index, account): account[0]
            for index, account in enumerate(accounts, 1)
        }
        for future in as_completed(futures):
            email = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                log(f"管线异常 email={email}: {exc}", "ERR")
                results.append((1, email))
    return results


# ---------------------------------------------------------------- 单轮
def run_once(args, env):
    """跑一轮 A+B。返回 Stage B 的 exit code（0=成功）；没拿到邮箱返回 1。"""
    t0 = time.time()
    # Stage A
    token = client_id = ""
    if args.skip_email:
        if not args.email:
            raise SystemExit("--skip-email 需要同时给 --email")
        email, password = args.email.strip(), args.password.strip()
        log(f"跳过邮箱注册，直接用 {email}", "A")
    else:
        got = stage_email(args, env)
        if not got:
            log("Stage A 没拿到可用邮箱，本轮终止", "ERR")
            return 1, ""
        email, password, token, client_id = got
        # emails.txt 里可能没记密码，用快照里的
        password = password or args.password

    # Stage B
    print("=" * 64)
    rc = stage_platforms(args, env, email, password, token, client_id)
    print("=" * 64)
    dt = time.time() - t0
    log(f"本轮结束  email={email}  Stage B exit={rc}  用时 {dt:.0f}s",
        "OK" if rc == 0 else "WARN")
    return rc, email


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="端到端全流程（邮箱注册 + 平台注册）")
    # Stage A
    ap.add_argument("--skip-email", action="store_true", help="跳过邮箱注册，直接用 --email")
    ap.add_argument("--email", default="", help="跳过邮箱注册时指定现成邮箱")
    ap.add_argument("--password", default="", help="配合 --email")
    ap.add_argument("--email-attempts", type=int, default=30, help="邮箱注册最多尝试次数")
    ap.add_argument("--email-timeout", type=int, default=180, help="单次邮箱注册硬超时(s)")
    ap.add_argument("--email-total-timeout", type=int, default=1800, help="Stage A 总超时(s)")
    ap.add_argument("--max-press", default="3", help="人机验证按住次数上限")
    ap.add_argument("--email-confirm-before-register", action="store_true",
                    help="邮箱注册页打开后自动点确认，再开始填写")
    # 循环
    ap.add_argument("--rounds", type=int, default=1,
                    help="循环注册轮数；1=只跑一次(默认)，0=无限循环(Ctrl+C 停)")
    ap.add_argument("--round-sleep", type=int, default=5, help="每轮之间间隔(s)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="同时运行的端到端邮箱管线数")
    # Stage B
    ap.add_argument("--platforms", nargs="+", choices=["claude", "chatgpt", "grok", "kiro", "github"],
                    default=["claude"], help="默认只跑 claude（最稳）；grok 已知死结")
    ap.add_argument("--node", default="auto", help="claude/chatgpt/grok 走的 Clash 节点")
    ap.add_argument(
        "--chatgpt-country", default="auto",
        help="ChatGPT 注册出口国家：auto 或两位 ISO 国家码",
    )
    ap.add_argument("--platform-timeout", type=int, default=600)
    ap.add_argument("--platform-retries", type=int, default=0,
                    help="单个平台失败后的额外重试次数")
    ap.add_argument("--broker", default="", help="共享取码服务URL；默认空=各脚本自行开 Outlook 取码")
    ap.add_argument("--grok-timeout", type=int, default=40,
                    help="Grok 共享邮箱 broker 取码超时(秒)")
    ap.add_argument("--keep-on-fail", action="store_true")
    ap.add_argument("--sequential-platforms", action="store_true",
                    help="按顺序运行所选平台；默认各平台并行")
    ap.add_argument("--import-c2a", action="store_true",
                    help="chatgpt 注册成功后即时把 token 导入 chatgpt2api（透传到底层 register_chatgpt.py）")
    ap.add_argument("--plus-subscription", action="store_true",
                    help="chatgpt 注册成功后加入本地 Plus 订阅工作台")
    ap.add_argument("--codex", action="store_true",
                    help="chatgpt 注册成功后走 Codex OAuth 提取 rt 导入 SUB2API（透传到底层 register_chatgpt.py）")
    ap.add_argument("--codex-group", default=None,
                    help="SUB2API 目标分组名（透传，默认取 config.SUB2API_GROUP）")
    ap.add_argument("--codex-manual-phone", action="store_true",
                    help="Codex add-phone 手动模式：不接码，自己在浏览器填号收码（透传）")
    ap.add_argument("--codex-sms-provider",
                    choices=["auto", "custom", "hero", "smsman", "firefox"],
                    default="auto", help="Codex add-phone 接码平台")
    ap.add_argument("--codex-timeout", type=int, default=120,
                    help="Codex OAuth 授权捕获超时(秒)")
    ap.add_argument("--codex-phone-skip", type=int, default=0,
                    help="正式接码前尝试免手机授权的次数")
    ap.add_argument("--codex-phone-attempts", type=int, default=2,
                    help="Codex add-phone 最大换号次数")
    ap.add_argument("--codex-sms-timeout", type=int, default=150,
                    help="Codex 单个手机号等待验证码秒数")
    ap.add_argument("--sms-get-phone-retries", type=int, default=4,
                    help="接码平台无库存时的取号重试次数")
    ap.add_argument("--custom-sms-pool-file", default="",
                    help="自定义接码号码池 JSON；留空使用 WebUI 已导入的默认池")
    ap.add_argument("--custom-sms-allowed-hosts", default="",
                    help="允许自定义接码记录域名使用非公网 DNS；逗号分隔，留空保持严格校验")
    ap.add_argument("--grok-sub2api", action="store_true",
                    help="Grok 注册成功后转为 SUB2API Grok OAuth 账号（透传）")
    ap.add_argument("--grok-sub2api-group", default=None,
                    help="SUB2API Grok 目标分组名（默认取 SUB2API_GROK_GROUP）")
    ap.add_argument("--grok-mailbox-attempts", type=int, default=6,
                    help="Grok 发码失败时的邮箱尝试次数")
    ap.add_argument("--claude-profile-retries", type=int, default=3,
                    help="Claude 住宅出口被拒时的新 Profile 总尝试次数")
    ap.add_argument("--claude-hcaptcha-retries", type=int, default=2,
                    help="Claude hCaptcha 自动求解尝试次数")
    ap.add_argument("--claude-challenge-wait", type=int, default=45,
                    help="Claude 每个节点等待 Cloudflare 自动通过的秒数")
    ap.add_argument("--claude-challenge-node-retries", type=int, default=3,
                    help="Claude 提交邮箱前的节点轮换次数")
    ap.add_argument("--claude-captcha-manual-timeout", type=int, default=0,
                    help="Claude 等待人工验证秒数；0 表示关闭")
    ap.add_argument("--skip-claude-validation", action="store_true",
                    help="跳过 Claude 对历史 sessionKey 的全量收尾校验")
    ap.add_argument("--kiro-account-password", default="",
                    help="Kiro Builder ID 密码；留空自动生成")
    ap.add_argument("--kiro-full-name", default="Test User",
                    help="Kiro Builder ID 显示名称")
    # 基建
    ap.add_argument("--proxy", default=PROXY_DEFAULT, help="HTTP(S)_PROXY；传空串禁用")
    ap.add_argument("--clash-api", default=CLASH_API_DEFAULT)
    ap.add_argument("--clash-secret", default=CLASH_SECRET_DEFAULT)
    ap.add_argument("--clash-group", default="GLOBAL",
                    help="Clash 组名（proxy_switch 探节点用）；global 模式下出口由 GLOBAL 决定，"
                         "传 'auto' 会 404。claude/grok 的节点选择模式见 --node")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    args = ap.parse_args()

    if args.concurrency < 1:
        raise SystemExit("--concurrency 必须大于等于 1")

    if args.skip_email and args.rounds != 1:
        # --skip-email 每轮都用同一个固定邮箱，循环没意义（甚至会重复注册同号）
        raise SystemExit("--skip-email 只能跑单轮，不能配合 --rounds")

    env = build_child_env(args)
    t_all = time.time()
    print("=" * 64)
    mode = "无限" if args.rounds == 0 else f"{args.rounds} 轮"
    platform_mode = "顺序" if args.sequential_platforms else "并行"
    traffic_mode = env.get("REG_FACTORY_RESIDENTIAL_TRAFFIC_MODE", "balanced")
    log(f"全流程开始（循环 {mode}）  proxy={args.proxy or 'OFF'}  clash={args.clash_api}"
        f"  concurrency={args.concurrency}  platforms={platform_mode}  traffic={traffic_mode}")
    print("=" * 64)

    ok = fail = 0
    rnd = 0
    last_rc = 0
    try:
        if args.skip_email:
            last_rc, _email = run_once(args, env)
            ok = int(last_rc == 0)
            fail = int(last_rc != 0)
            rnd = 1
        while args.rounds == 0 or rnd < args.rounds:
            if args.skip_email:
                break
            remaining = args.concurrency if args.rounds == 0 else args.rounds - rnd
            wave_target = min(args.concurrency, remaining)
            print("#" * 64)
            log(f"===== 并发波次开始 completed={rnd} target={wave_target} =====")
            print("#" * 64)
            results = run_wave(args, env, wave_target)
            for rc, _email in results:
                rnd += 1
                last_rc = rc
                if rc == 0:
                    ok += 1
                else:
                    fail += 1
            # 最后一轮不必再睡
            more = args.rounds == 0 or rnd < args.rounds
            if more and args.round_sleep > 0 and not args.dry_run:
                log(f"本轮完成，{args.round_sleep}s 后进入下一轮（Ctrl+C 可停）")
                time.sleep(args.round_sleep)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，停止循环", "WARN")

    dt = time.time() - t_all
    print("=" * 64)
    log(f"全部结束  共 {rnd} 轮  成功 {ok}  失败 {fail}  总用时 {dt:.0f}s",
        "OK" if fail == 0 and ok > 0 else "WARN")
    # 退出码：全成功 0；否则沿用最后一轮的非零码（单轮场景行为不变）
    return 0 if fail == 0 and ok > 0 else (last_rc or 1)


if __name__ == "__main__":
    sys.exit(main())
