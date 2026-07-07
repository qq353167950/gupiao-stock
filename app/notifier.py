"""
多渠道消息推送模块
支持渠道：Server酱 / PushPlus / 钉钉机器人 / 企业微信机器人 / Telegram Bot / SMTP 邮件

设计约定：
- 渠道由环境变量 NOTIFY_CHANNELS 控制（逗号分隔），凭据缺失的渠道自动跳过并告警
- 所有渠道统一收 (title, content) 二元组，content 为 Markdown 文本
  （不支持 Markdown 的渠道自动降级为纯文本）
- 单渠道失败不影响其他渠道，返回逐渠道结果便于日志排查
"""
import hashlib
import hmac
import base64
import time
import urllib.parse
from typing import Dict, List, Optional

import requests

from app.config import (
    NOTIFY_CHANNELS,
    SERVERCHAN_SENDKEY,
    PUSHPLUS_TOKEN,
    DINGTALK_WEBHOOK,
    DINGTALK_SECRET,
    WECOM_WEBHOOK,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_USER,
    SMTP_PASSWORD,
    SMTP_TO,
    PUBLIC_BASE_URL,
)

REQUEST_TIMEOUT = 15  # 推送接口统一超时（秒）
RETRY_DELAY = 5  # 失败重试间隔（秒）


def _send_with_retry(sender, title: str, content: str, channel: str) -> bool:
    """执行单渠道推送，失败后重试一次（应对瞬时网络抖动）。

    凭据缺失（返回 False 且未抛异常）不重试——重试也不会成功。
    """
    try:
        return sender(title, content)
    except Exception as e:
        print(f"   ⚠️  推送 [{channel}] 首次失败: {e}，{RETRY_DELAY}s 后重试...")
        time.sleep(RETRY_DELAY)
        return sender(title, content)


def _markdown_to_plain(text: str) -> str:
    """Markdown 降级为纯文本（供 Telegram/纯文本渠道使用）"""
    plain = text
    for token in ("**", "__", "`", "#"):
        plain = plain.replace(token, "")
    return plain


def _send_serverchan(title: str, content: str) -> bool:
    """Server酱 Turbo（https://sct.ftqq.com），微信服务号推送"""
    if not SERVERCHAN_SENDKEY:
        print("   ⚠️  serverchan: 未配置 SERVERCHAN_SENDKEY，跳过")
        return False
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send"
    resp = requests.post(
        url,
        data={"title": title[:32], "desp": content},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    ok = resp.json().get("code") == 0
    if not ok:
        print(f"   ⚠️  serverchan 返回异常: {resp.text[:200]}")
    return ok


def _send_pushplus(title: str, content: str) -> bool:
    """PushPlus（https://www.pushplus.plus），微信公众号推送"""
    if not PUSHPLUS_TOKEN:
        print("   ⚠️  pushplus: 未配置 PUSHPLUS_TOKEN，跳过")
        return False
    resp = requests.post(
        "https://www.pushplus.plus/send",
        json={
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "markdown",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    ok = resp.json().get("code") == 200
    if not ok:
        print(f"   ⚠️  pushplus 返回异常: {resp.text[:200]}")
    return ok


def _dingtalk_signed_url() -> str:
    """钉钉加签：官方 HmacSHA256 签名算法"""
    url = DINGTALK_WEBHOOK
    if DINGTALK_SECRET:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
        sign = base64.b64encode(
            hmac.new(
                DINGTALK_SECRET.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        )
        url = f"{url}&timestamp={timestamp}&sign={urllib.parse.quote_plus(sign)}"
    return url


def _send_dingtalk(title: str, content: str) -> bool:
    """钉钉群机器人（markdown 消息，支持加签）"""
    if not DINGTALK_WEBHOOK:
        print("   ⚠️  dingtalk: 未配置 DINGTALK_WEBHOOK，跳过")
        return False
    resp = requests.post(
        _dingtalk_signed_url(),
        json={
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"## {title}\n\n{content}"},
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    ok = resp.json().get("errcode") == 0
    if not ok:
        print(f"   ⚠️  dingtalk 返回异常: {resp.text[:200]}")
    return ok


def _send_wecom(title: str, content: str) -> bool:
    """企业微信群机器人（markdown 消息，单条上限 4096 字节）"""
    if not WECOM_WEBHOOK:
        print("   ⚠️  wecom: 未配置 WECOM_WEBHOOK，跳过")
        return False
    text = f"## {title}\n\n{content}"
    # 企业微信 markdown 内容上限 4096 字节，超长截断
    while len(text.encode("utf-8")) > 4000:
        text = text[: int(len(text) * 0.9)]
    resp = requests.post(
        WECOM_WEBHOOK,
        json={"msgtype": "markdown", "markdown": {"content": text}},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    ok = resp.json().get("errcode") == 0
    if not ok:
        print(f"   ⚠️  wecom 返回异常: {resp.text[:200]}")
    return ok


def _send_telegram(title: str, content: str) -> bool:
    """Telegram Bot（纯文本，规避 Markdown 转义问题；上限 4096 字符）"""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("   ⚠️  telegram: 未配置 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID，跳过")
        return False
    text = f"{title}\n\n{_markdown_to_plain(content)}"[:4000]
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    ok = resp.json().get("ok", False)
    if not ok:
        print(f"   ⚠️  telegram 返回异常: {resp.text[:200]}")
    return ok


def _send_email(title: str, content: str) -> bool:
    """SMTP 邮件（SSL 465 / STARTTLS 587 自适应）"""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_TO):
        print("   ⚠️  email: SMTP 配置不完整，跳过")
        return False
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    from email.utils import formataddr

    # Markdown 简单转 HTML（换行与加粗），保证邮件客户端可读
    html_body = _markdown_to_plain(content).replace("\n", "<br>")
    msg = MIMEText(f"<html><body>{html_body}</body></html>", "html", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr(("股票分析平台", SMTP_USER))
    recipients = [addr.strip() for addr in SMTP_TO.split(",") if addr.strip()]
    msg["To"] = ", ".join(recipients)

    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT)
        server.starttls()
    try:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, recipients, msg.as_string())
    finally:
        server.quit()
    return True


# 渠道注册表：新增渠道只需实现 _send_xxx 并在此登记
_CHANNEL_SENDERS = {
    "serverchan": _send_serverchan,
    "pushplus": _send_pushplus,
    "dingtalk": _send_dingtalk,
    "wecom": _send_wecom,
    "telegram": _send_telegram,
    "email": _send_email,
}


def send(title: str, content: str) -> Dict[str, bool]:
    """向所有已配置渠道推送消息。

    Args:
        title: 消息标题
        content: Markdown 格式正文

    Returns:
        {渠道名: 是否成功}；未配置任何渠道时返回空 dict
    """
    if not NOTIFY_CHANNELS:
        print("ℹ️  未配置 NOTIFY_CHANNELS，跳过推送")
        return {}

    results: Dict[str, bool] = {}
    for channel in NOTIFY_CHANNELS:
        sender = _CHANNEL_SENDERS.get(channel)
        if not sender:
            print(f"   ⚠️  未知推送渠道: {channel}（可选: {', '.join(_CHANNEL_SENDERS)}）")
            results[channel] = False
            continue
        try:
            results[channel] = _send_with_retry(sender, title, content, channel)
            status = "✓" if results[channel] else "✗"
            print(f"   {status} 推送 [{channel}]")
        except Exception as e:
            print(f"   ✗ 推送 [{channel}] 重试后仍失败: {e}")
            results[channel] = False
    return results


def format_daily_digest(date: str, recommendations: List[dict]) -> str:
    """将当日推荐列表格式化为 Markdown 摘要。

    Args:
        date: 推荐日期 YYYY-MM-DD
        recommendations: DailyRecommendation.to_dict() 列表（需含 sector 字段）

    Returns:
        Markdown 文本
    """
    if not recommendations:
        return f"**{date}** 暂无推荐（分析可能未完成或无股票通过筛选）"

    # 按板块分组
    sectors: Dict[str, List[dict]] = {}
    for rec in recommendations:
        sector = rec.get("sector", "其他")
        sectors.setdefault(sector, []).append(rec)

    lines = [f"📅 **{date} 股票推荐**（共 {len(recommendations)} 只）", ""]
    for sector, recs in sectors.items():
        lines.append(f"**【{sector}】**")
        for i, rec in enumerate(recs, 1):
            emoji = "🥇" if i == 1 else "⭐"
            score = rec.get("score")
            score_text = f"{score:.1f}" if isinstance(score, (int, float)) else "—"
            name = rec.get("name") or rec.get("ticker", "")
            lines.append(f"{emoji} {name}（{rec.get('ticker', '')}）评分 {score_text}")
            reason = rec.get("reason")
            if reason:
                lines.append(f"   {reason}")
            risk = rec.get("risk_level")
            if risk:
                lines.append(f"   风险等级: {risk}")
        lines.append("")

    lines.append(f"🌐 查看完整报告: {PUBLIC_BASE_URL}")
    lines.append("")
    lines.append("⚠️ 以上内容仅供参考，不构成投资建议")
    return "\n".join(lines)


def format_analysis_summary(
    date: str,
    total: int,
    completed: int,
    failed: int,
    top_stocks: Optional[List[dict]] = None,
) -> str:
    """分析批次完成摘要（auto_analyze 完成后推送）"""
    lines = [
        f"📊 **{date} 批量分析完成**",
        "",
        f"- 任务总数: {total}",
        f"- 成功: {completed}",
        f"- 失败/超时: {failed}",
        "",
    ]
    if top_stocks:
        lines.append("**综合评分 TOP 5：**")
        for i, stock in enumerate(top_stocks[:5], 1):
            score = stock.get("score")
            score_text = f"{score:.1f}" if isinstance(score, (int, float)) else "—"
            lines.append(
                f"{i}. {stock.get('name', '')}（{stock.get('ticker', '')}）{score_text} 分"
            )
        lines.append("")
    lines.append(f"🌐 平台: {PUBLIC_BASE_URL}")
    return "\n".join(lines)
