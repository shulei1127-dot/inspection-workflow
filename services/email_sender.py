"""Email sender module extracted from the Streamlit inspection email tool.

Core functionality:
- extract_info_with_ai(): Use ZhipuAI to extract info from PDF text
- send_email(): SMTP_SSL email sending
- pypinyin name-to-email conversion
"""

import json
import logging
import os
import smtplib
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from core.config import get_settings

logger = logging.getLogger(__name__)


def _get_name_pinyin(name: str) -> str:
    """Convert Chinese name to pinyin email format: 舒磊 -> lei.shu@chaitin.com, 杨振兴 -> zhenxing.yang@chaitin.com"""
    try:
        from pypinyin import pinyin, Style
        parts = pinyin(name, style=Style.NORMAL)
        if len(parts) >= 2:
            surname = parts[0][0]
            given = "".join(p[0] for p in parts[1:])
            return f"{given}.{surname}@chaitin.com"
        elif len(parts) == 1:
            return f"{parts[0][0]}@chaitin.com"
    except ImportError:
        logger.warning("pypinyin not installed, cannot convert name to email")
    return ""


def extract_info_with_ai(text: str) -> tuple[dict | None, str | None]:
    """Use ZhipuAI to extract structured info from inspection report text.

    Returns (info_dict, error_message)
    """
    settings = get_settings()
    api_key = settings.ai_api_key or os.getenv("AI_API_KEY", "")
    if not api_key:
        return None, "未配置 AI API Key"

    try:
        from zhipuai import ZhipuAI

        prompt = f"""你是一个信息提取助手。请从以下巡检报告文本中提取以下信息：
1. 客户名称（公司全称）
2. 产品名称（谛听/洞鉴/雷池等）
3. 巡检时间（格式化为 YYYY-MM-DD）。注意：PDF中可能包含模板创建日期（通常出现在页眉或封面副标题，格式较旧如2024年），这不是实际巡检时间。实际巡检时间通常出现在报告标题附近或正文首段，且应是最近的日期。请优先选择标题旁或正文首段出现的日期，而非页眉/封面中较旧的模板日期。
4. 巡检数量（如"1套"、"4台"等，保留数字和单位）
5. 客户邮箱（可能有多个，也可能没有）
6. 巡检总结（报告中的总结段落，完整提取）

请严格按以下 JSON 格式返回，不要包含任何其他内容：
{{"customer_name": "","product_name": "","inspection_date": "","quantity": "","emails": [],"summary": ""}}

巡检报告内容：
{text}"""

        client = ZhipuAI(api_key=api_key)
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        content = response.choices[0].message.content.strip()
        # Clean markdown code blocks
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        return json.loads(content), None
    except Exception as e:
        return None, f"AI 提取失败: {e}"


def send_email(
    to_emails: list[str],
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes]] | None = None,
    cc_emails: str = "",
    body_type: str = "plain",
) -> tuple[bool, str]:
    """Send email via SMTP_SSL.

    Args:
        to_emails: List of recipient email addresses
        subject: Email subject
        body: Email body
        attachments: List of (filename, bytes) tuples
        cc_emails: Comma-separated CC addresses
        body_type: "plain" or "html"

    Returns (success, message)
    """
    settings = get_settings()
    sender_email = settings.inspection_email_sender
    sender_password = settings.inspection_email_password
    smtp_host = settings.inspection_email_smtp_host
    smtp_port = settings.inspection_email_smtp_port

    if not sender_email or not sender_password:
        return False, "SMTP 配置不完整"

    try:
        msg = MIMEMultipart()
        msg["From"] = formataddr((str(Header("长亭科技", "utf-8")), sender_email))
        msg["To"] = ", ".join(to_emails)
        if cc_emails:
            msg["Cc"] = cc_emails
        msg["Subject"] = Header(subject, "utf-8")

        msg.attach(MIMEText(body, body_type, "utf-8"))

        if attachments:
            for attachment_name, attachment_bytes in attachments:
                attachment = MIMEApplication(attachment_bytes)
                attachment.add_header("Content-Disposition", "attachment", filename=attachment_name)
                msg.attach(attachment)

        all_recipients = to_emails + ([e.strip() for e in cc_emails.split(",") if e.strip()] if cc_emails else [])

        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, msg.as_bytes())

        return True, "发送成功"
    except Exception as e:
        return False, f"发送失败: {e}"


def send_inspection_email(
    customer_name: str = "",
    product_name: str = "",
    to_emails: list[str] | None = None,
    body: str = "",
    attachments: list[tuple[str, bytes]] | None = None,
) -> dict:
    """High-level function to send an inspection notification email.

    If to_emails is not provided, attempts to derive from engineer name.
    """
    if not to_emails:
        to_emails = []

    if not body:
        body = f"尊敬的客户，\n\n本次巡检服务已完成。以下是巡检概要：\n\n客户：{customer_name}\n产品：{product_name}\n\n详细报告请见附件。\n\n此致\n长亭科技"

    subject = f"巡检报告 - {customer_name} - {product_name}"

    success, message = send_email(
        to_emails=to_emails,
        subject=subject,
        body=body,
        attachments=attachments,
    )

    return {"success": success, "message": message, "subject": subject}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Send inspection email")
    parser.add_argument("--customer", required=True, help="Customer name")
    parser.add_argument("--product", required=True, help="Product name")
    parser.add_argument("--to", nargs="+", help="Recipient emails")
    parser.add_argument("--pdf-path", help="PDF attachment path")
    args = parser.parse_args()

    attachments = []
    if args.pdf_path:
        with open(args.pdf_path, "rb") as f:
            attachments.append((os.path.basename(args.pdf_path), f.read()))

    result = send_inspection_email(
        customer_name=args.customer,
        product_name=args.product,
        to_emails=args.to,
        attachments=attachments or None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
