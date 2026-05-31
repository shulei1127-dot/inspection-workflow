import streamlit as st
import fitz
import json
import os
import re
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import formataddr
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None
from zhipuai import ZhipuAI
import tempfile
import io

# Load .env from project root (parent directory)
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.json"

COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "梁丘", "左丘", "东门", "西门", "南宫",
}

DEFAULT_CONFIG = {
    "sender_email": "",
    "sender_password": "",
    "sender_name": "长亭科技",
    "cc_emails": "",
    "smtp_server": "smtpdm.aliyun.com",
    "smtp_port": 465,
}

def load_config():
    # Start with defaults
    config = DEFAULT_CONFIG.copy()

    # Overlay from project .env (INSPECTION_EMAIL_* variables)
    env_map = {
        "sender_email": "INSPECTION_EMAIL_SENDER",
        "sender_password": "INSPECTION_EMAIL_PASSWORD",
        "smtp_server": "INSPECTION_EMAIL_SMTP_HOST",
    }
    for key, env_var in env_map.items():
        val = os.getenv(env_var, "")
        if val:
            config[key] = val
    smtp_port = os.getenv("INSPECTION_EMAIL_SMTP_PORT", "")
    if smtp_port:
        config["smtp_port"] = int(smtp_port)

    # Overlay from local config.json (user edits in sidebar override .env)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            local_config = json.load(f)
        for key, value in local_config.items():
            if value:  # non-empty values from config.json take priority
                config[key] = value

    return config

def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def load_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def contains_chinese(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text))

def split_recipient_tokens(raw_value):
    return [token.strip() for token in re.split(r"[,，；;\n]+", raw_value or "") if token.strip()]

def convert_chinese_name_to_email(name, domain="chaitin.com"):
    normalized_name = re.sub(r"\s+", "", name or "")
    if not normalized_name or not contains_chinese(normalized_name) or "@" in normalized_name:
        return normalized_name
    if not re.fullmatch(r"[\u4e00-\u9fff]+", normalized_name):
        return normalized_name
    if lazy_pinyin is None or len(normalized_name) < 2:
        return normalized_name

    surname_length = 2 if len(normalized_name) > 2 and normalized_name[:2] in COMPOUND_SURNAMES else 1
    surname = normalized_name[:surname_length]
    given_name = normalized_name[surname_length:]
    if not given_name:
        return normalized_name

    surname_pinyin = "".join(lazy_pinyin(surname))
    given_name_pinyin = "".join(lazy_pinyin(given_name))
    return f"{given_name_pinyin}.{surname_pinyin}@{domain}".lower()

def normalize_recipient_input(raw_value):
    normalized_tokens = []
    seen_tokens = set()
    for token in split_recipient_tokens(raw_value):
        normalized_token = convert_chinese_name_to_email(token)
        dedupe_key = normalized_token.lower()
        if normalized_token and dedupe_key not in seen_tokens:
            normalized_tokens.append(normalized_token)
            seen_tokens.add(dedupe_key)
    return ", ".join(normalized_tokens)

def normalize_recipient_emails_in_state():
    st.session_state.recipient_input = normalize_recipient_input(st.session_state.get("recipient_input", ""))

def extract_pdf_text(pdf_file):
    try:
        pdf_bytes = pdf_file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for page in doc:
            text += page.get_text()
        pdf_file.seek(0)
        return text
    except Exception as e:
        return None

def extract_info_with_ai(text):
    api_key = os.getenv("AI_API_KEY")
    if not api_key:
        return None, "未配置 AI API Key，请在 .env 文件中配置"
    
    prompt = f"""你是一个信息提取助手。请从以下巡检报告文本中提取以下信息：
1. 客户名称（公司全称）
2. 产品名称（谛听/洞鉴/雷池等）
3. 巡检时间（格式化为 YYYY-MM-DD）
4. 巡检数量（如"1套"、"4台"等，保留数字和单位）
5. 客户邮箱（可能有多个，也可能没有）
6. 巡检总结（报告中的总结段落，完整提取）

请严格按以下 JSON 格式返回，不要包含任何其他内容：
{{"customer_name": "","product_name": "","inspection_date": "","quantity": "","emails": [],"summary": ""}}

巡检报告内容：
{text}"""
    
    try:
        client = ZhipuAI(api_key=api_key)
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        return json.loads(content), None
    except Exception as e:
        return None, f"AI 提取失败: {str(e)}"

def send_email(sender_email, sender_password, sender_name, smtp_server, smtp_port, to_emails, cc_emails, subject, body, attachments):
    try:
        msg = MIMEMultipart()
        msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender_email))
        msg["To"] = ", ".join(to_emails)
        if cc_emails:
            msg["Cc"] = cc_emails
        msg["Subject"] = Header(subject, "utf-8")
        
        msg.attach(MIMEText(body, "plain", "utf-8"))
        
        for attachment_name, attachment_bytes in attachments:
            attachment = MIMEApplication(attachment_bytes)
            attachment.add_header("Content-Disposition", "attachment", filename=attachment_name)
            msg.attach(attachment)
        
        all_recipients = to_emails + ([e.strip() for e in cc_emails.split(",") if e.strip()] if cc_emails else [])
        
        with smtplib.SMTP_SSL(smtp_server, int(smtp_port)) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, all_recipients, msg.as_string())
        
        return True, "发送成功"
    except Exception as e:
        return False, f"发送失败: {str(e)}"

st.set_page_config(page_title="巡检报告邮件自动化", page_icon="📧", layout="wide")

st.title("📧 巡检报告邮件自动化工具")
st.markdown("上传 PDF 巡检报告（支持多个），AI 自动提取信息并发送邮件")

config = load_config()

with st.sidebar:
    st.header("⚙️ 发件人配置")
    sender_email = st.text_input("发件人邮箱", value=config.get("sender_email", ""), placeholder="your@dingtalk.com")
    sender_password = st.text_input("授权码", value=config.get("sender_password", ""), type="password", help="在钉钉邮箱设置中生成授权码")
    sender_name = st.text_input("显示名称", value=config.get("sender_name", "长亭科技"))
    smtp_server = st.text_input("SMTP 服务器", value=config.get("smtp_server", "smtpdm.aliyun.com"), placeholder="smtp.qq.com")
    smtp_port = st.number_input("SMTP 端口", min_value=1, max_value=65535, value=int(config.get("smtp_port", 465)), step=1)
    cc_emails = st.text_input("抄送邮箱", value=config.get("cc_emails", ""), placeholder="多个邮箱用逗号分隔")
    
    if st.button("保存配置"):
        new_config = {
            "sender_email": sender_email,
            "sender_password": sender_password,
            "sender_name": sender_name,
            "smtp_server": smtp_server.strip(),
            "smtp_port": int(smtp_port),
            "cc_emails": cc_emails
        }
        save_config(new_config)
        st.success("配置已保存")

st.divider()

st.header("📄 上传巡检报告（支持多个）")
uploaded_files = st.file_uploader("上传 PDF 文件", type=["pdf"], accept_multiple_files=True)

if uploaded_files:
    st.info(f"已上传 {len(uploaded_files)} 个文件")
    for f in uploaded_files:
        st.text(f"  • {f.name} ({f.size / 1024:.1f} KB)")

if uploaded_files:
    if "extracted_reports" not in st.session_state or st.session_state.get("last_files_count") != len(uploaded_files):
        with st.spinner("正在分析 PDF..."):
            extracted_reports = []
            all_emails = []
            
            for pdf_file in uploaded_files:
                pdf_file.seek(0)
                pdf_text = extract_pdf_text(pdf_file)
                pdf_file.seek(0)
                
                if pdf_text:
                    info, error = extract_info_with_ai(pdf_text)
                    if info:
                        extracted_reports.append({
                            "file_name": pdf_file.name,
                            "info": info
                        })
                        if info.get("emails"):
                            all_emails.extend(info["emails"])
                    else:
                        st.warning(f"⚠️ {pdf_file.name}: {error}")
                        extracted_reports.append({
                            "file_name": pdf_file.name,
                            "info": {"customer_name": "", "product_name": "", "inspection_date": "", "quantity": "", "emails": [], "summary": ""}
                        })
                else:
                    st.warning(f"⚠️ {pdf_file.name}: 无法读取 PDF")
                    extracted_reports.append({
                        "file_name": pdf_file.name,
                        "info": {"customer_name": "", "product_name": "", "inspection_date": "", "quantity": "", "emails": [], "summary": ""}
                    })
            
            st.session_state.extracted_reports = extracted_reports
            st.session_state.all_emails = list(set(all_emails))
            st.session_state.last_files_count = len(uploaded_files)
    
    extracted_reports = st.session_state.extracted_reports
    all_emails = st.session_state.all_emails
    
    st.divider()
    st.header("🤖 AI 提取结果")
    
    default_customer = extracted_reports[0]["info"].get("customer_name", "") if extracted_reports else ""
    default_date = extracted_reports[0]["info"].get("inspection_date", "") if extracted_reports else ""
    
    customer_name = st.text_input("客户名称", value=default_customer)
    
    product_names = [r["info"].get("product_name", "") for r in extracted_reports if r["info"].get("product_name")]
    product_name_str = "、".join(product_names) if product_names else ""
    product_name = st.text_input("产品名称", value=product_name_str, help="多个产品用顿号分隔")
    
    inspection_date = st.text_input("巡检时间", value=default_date)
    
    products_with_quantity = []
    for r in extracted_reports:
        qty = r["info"].get("quantity", "")
        prod = r["info"].get("product_name", "")
        if qty and prod:
            products_with_quantity.append(f"{qty}{prod}")
        elif prod:
            products_with_quantity.append(prod)
    products_str = "、".join(products_with_quantity) if products_with_quantity else ""
    
    quantity = st.text_input("数量", value=products_str, help="数量+产品名称配对显示")
    
    emails_str = ", ".join(all_emails) if all_emails else ""
    default_recipient_emails = normalize_recipient_input(emails_str)
    if "recipient_input" not in st.session_state or st.session_state.get("last_default_recipient_emails") != default_recipient_emails:
        st.session_state.recipient_input = default_recipient_emails
        st.session_state.last_default_recipient_emails = default_recipient_emails

    recipient_emails = st.text_input(
        "收件人邮箱",
        key="recipient_input",
        placeholder="支持输入邮箱或中文姓名，多个用逗号分隔",
        on_change=normalize_recipient_emails_in_state,
    )
    st.caption("可直接输入中文姓名并自动转为长亭邮箱，例如：舒磊 -> lei.shu@chaitin.com，汪丹丹 -> dandan.wang@chaitin.com")
    
    if not all_emails:
        st.warning("⚠️ 未检测到客户邮箱，请手动添加")
    
    st.subheader("各产品巡检总结")
    summaries = []
    for i, report in enumerate(extracted_reports):
        product = report["info"].get("product_name", f"产品{i+1}")
        summary_text = report["info"].get("summary", "")
        edited_summary = st.text_area(f"{product} 总结", value=summary_text, height=100, key=f"summary_{i}")
        summaries.append({"product": product, "summary": edited_summary})
    
    st.divider()
    st.header("📋 邮件内容（可编辑）")
    
    if len(extracted_reports) == 1:
        combined_summary = summaries[0]["summary"] if summaries else ""
    else:
        combined_summary = "\n\n".join([f"【{s['product']}】\n{s['summary']}" for s in summaries if s['summary']])
    
    default_subject = f"【长亭科技巡检报告】- {customer_name}-{product_name}-{inspection_date}" if customer_name else "【长亭科技巡检报告】"
    default_body = f"""尊敬的客户，您好，

非常感谢对长亭科技的信任！本司于 {inspection_date} 对贵司的 {quantity} 进行了一次全面的巡检，结果如下：

{combined_summary}

详细巡检报告见附件，请查收！

后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～"""
    
    if "email_subject" not in st.session_state or st.session_state.get("last_default_subject") != default_subject:
        st.session_state.email_subject = default_subject
        st.session_state.last_default_subject = default_subject
    
    if "email_body" not in st.session_state or st.session_state.get("last_default_body") != default_body:
        st.session_state.email_body = default_body
        st.session_state.last_default_body = default_body
    
    col_sub, col_reset = st.columns([5, 1])
    with col_sub:
        st.subheader("邮件标题")
    with col_reset:
        if st.button("恢复默认", key="reset_btn", help="恢复到自动生成的标题和正文"):
            st.session_state.email_subject = default_subject
            st.session_state.email_body = default_body
            st.rerun()
    
    subject = st.text_input("邮件标题", value=st.session_state.email_subject, label_visibility="collapsed")
    
    st.subheader("邮件正文")
    body = st.text_area("邮件正文", value=st.session_state.email_body, height=300, label_visibility="collapsed")
    
    st.session_state.email_subject = subject
    st.session_state.email_body = body
    
    st.divider()
    st.header("🚀 发送邮件")
    
    col_send1, col_send2, col_send3 = st.columns([2, 2, 1])
    
    with col_send1:
        if st.button("📤 发送邮件", type="primary", use_container_width=True):
            if not sender_email or not sender_password:
                st.error("❌ 请先配置发件人邮箱和授权码")
            elif not recipient_emails:
                st.error("❌ 请填写收件人邮箱")
            elif not customer_name or not product_name:
                st.error("❌ 请填写客户名称和产品名称")
            else:
                with st.spinner("发送中..."):
                    attachments = []
                    for pdf_file in uploaded_files:
                        pdf_file.seek(0)
                        attachments.append((pdf_file.name, pdf_file.read()))
                    
                    to_emails = [e.strip() for e in recipient_emails.split(",") if e.strip()]
                    success, message = send_email(
                        sender_email, sender_password, sender_name, smtp_server, smtp_port,
                        to_emails, cc_emails, subject, body,
                        attachments
                    )
                    
                    if success:
                        st.success(f"✅ {message}")
                        history = load_history()
                        history.insert(0, {
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "to": recipient_emails,
                            "customer": customer_name,
                            "product": product_name,
                            "files": len(uploaded_files),
                            "status": "成功"
                        })
                        history = history[:50]
                        save_history(history)
                    else:
                        st.error(f"❌ {message}")
    
    with col_send2:
        if st.button("🔄 重新提取", use_container_width=True):
            if "extracted_reports" in st.session_state:
                del st.session_state.extracted_reports
            if "last_files_count" in st.session_state:
                del st.session_state.last_files_count
            st.rerun()
    
    with col_send3:
        if st.button("🗑️ 清空", use_container_width=True):
            if "extracted_reports" in st.session_state:
                del st.session_state.extracted_reports
            if "last_files_count" in st.session_state:
                del st.session_state.last_files_count
            st.rerun()

else:
    st.info("请先上传 PDF 文件")
    
    st.divider()
    st.header("📋 邮件内容（可编辑）")
    
    default_subject = "【长亭科技巡检报告】"
    default_body = """尊敬的客户，您好，

非常感谢对长亭科技的信任！本司于 {时间} 对贵司的 {数量} {产品名称} 进行了一次全面的巡检，结果如下：

{巡检总结}

详细巡检报告见附件，请查收！

后续如有问题欢迎通过【长亭科技售后服务中心】微信服务号-【人工服务】联系我们～"""
    
    subject = st.text_input("邮件标题", value=default_subject, label_visibility="collapsed")
    body = st.text_area("邮件正文", value=default_body, height=300, label_visibility="collapsed")
    
    st.divider()
    st.header("🚀 发送邮件")
    
    col_send1, col_send2, col_send3 = st.columns([2, 2, 1])
    
    with col_send1:
        st.button("📤 发送邮件", type="primary", disabled=True, use_container_width=True)
    
    with col_send2:
        if st.button("🔄 重新提取", use_container_width=True):
            st.rerun()
    
    with col_send3:
        if st.button("🗑️ 清空", use_container_width=True):
            st.rerun()

st.divider()

with st.expander("📜 发送记录（最近 10 条）"):
    history = load_history()
    if history:
        for record in history[:10]:
            files_info = f"({record.get('files', 1)}个附件)" if record.get('files', 1) > 1 else ""
            st.markdown(f"- **{record.get('time', '')}** | {record.get('customer', '')} | {record.get('product', '')} | → {record.get('to', '')} | {record.get('status', '')} {files_info}")
    else:
        st.info("暂无发送记录")
