"""Yunji dispatch logic: create requirements on yunji.chaitin.cn.

Migrated from server.js — replaces Puppeteer-based approach:
- PTS data: GraphQL API (pts_client.py)
- Yunji API: direct HTTP with session cookie (yunji_client.py)
- Static mappings: SUPPLIER_MAP, ASSIGNER_DEPT, DEPT_LEADER, DEFAULTS
"""

import asyncio
import logging
import re
import time
from datetime import datetime

from services import pts_client
from services import yunji_client

logger = logging.getLogger(__name__)

# ── Static Mappings (from server.js) ──────────────────────────────────────

def _parse_supplier_short_name(name: str) -> str:
    """Extract supplier short name from AITable format '短名-地区'.

    AITable stores supplier as e.g. '腾云-深圳', '平云-全国'.
    SUPPLIER_MAP keys are short names: '腾云', '平云', etc.
    """
    if "-" in name:
        return name.split("-")[0]
    return name


SUPPLIER_MAP = {
    "平云": "成都平云小匠网络有限公司",
    "腾云": "深圳市腾云智服科技有限公司",
    "禹赫": "上海禹赫信息技术有限公司",
    "中朔": "上海中朔信息科技有限公司",
    "迈望": "上海迈望信息技术有限公司",
    "图融": "上海图融信息技术有限公司",
    "微中鑫": "南宁市微中鑫电子科技有限公司",
    "华宇": "河北华宇信息技术有限公司",
    "云申": "山东云申智能科技有限公司",
    "德瑞": "深圳市德瑞信息技术有限公司",
}

ASSIGNER_DEPT = {
    "刘超": "华北东北技术支持",
    "吴冬兵": "华南技术支持",
    "王刚": "解决方案商业化组",
    "李东方": "华东技术支持",
    "李升明": "西南西北技术支持",
    "张镇朝": "华北东北技术支持",
    "王德鑫": "政府央企能源行业技术支持",
    "田英超": "华东技术支持",
    "高云松": "华东技术支持",
    "陈祎雯": "华东技术支持",
    "王欣": "通信行业技术支持",
    "彭明豪": "华南技术支持",
    "陈欧翔": "华东技术支持",
    "李真真": "华南技术支持",
    "杨伦": "华中技术支持",
    "王瑞": "华东技术支持",
    "郑思成": "华南技术支持",
    "仇鑫杰": "华北东北技术支持",
    "郭文祥": "华北东北技术支持",
}

DEPT_LEADER = {
    "华东技术支持": "黄彬",
    "华南技术支持": "郑义全",
    "华中技术支持": "杨伦",
    "西南西北技术支持": "栗永顺",
    "华北东北技术支持": "刘超",
    "政府央企能源行业技术支持": "刘超",
    "通信行业技术支持": "郑义全",
    "解决方案商业化组": "黄彬",
}

DEFAULTS = {
    "outsource_specialist": "徐丛然",
    "designated_scenario": "产品售后",
    "outsource_method": "部分外包",
    "delivery_content": "产品巡检",
    "deliverable": "巡检报告",
    "headcount": 1,
    "total_man_days": 1,
    "unit_price": 600,
    "need_interview": "否",
    "travel_budget": 0,
}


# ── PTS Data via GraphQL ──────────────────────────────────────────────────


async def fetch_pts_info(pts_order_id: str, db=None) -> dict:
    """Fetch PTS work order info for yunji dispatch.

    Strategy:
    1. Try local DB (WorkOrder.raw_data has delivery info from sync)
    2. If project.id missing, query PTS GraphQL API

    Returns dict with:
        - company_name: str
        - delivery_id: str (交付 ID)
        - project_id: str (项目 ID, used as crmProjectId for yunji)
        - project_name: str
        - assigner_name: str (交付分配人)
        - desc: str (工单描述)
    """
    # Strategy 1: Try local DB
    if db:
        from models.work_order import WorkOrder
        wo = db.query(WorkOrder).filter(WorkOrder.pts_order_id == pts_order_id).first()
        if wo and wo.raw_data:
            raw = wo.raw_data
            delivery = raw.get("delivery") or {}
            project = delivery.get("project") or {}
            assigner = delivery.get("assigner") or {}

            result = {
                "company_name": wo.customer_name or "",
                "delivery_id": delivery.get("id", ""),
                "project_id": project.get("id", ""),
                "project_name": project.get("name", ""),
                "assigner_name": wo.assigner_name or assigner.get("name", ""),
                "desc": raw.get("desc", ""),
            }

            # If we have project.id, we're good
            if result["project_id"]:
                return result

            # If no project.id, fall through to PTS API query

    # Strategy 2: Query PTS GraphQL API directly by ID
    query = """
    {
      workOrderByID(id: "%s") {
        id
        desc
        company { id name }
        delivery {
          id
          project { id name }
          assigner { id name username }
        }
      }
    }
    """ % pts_order_id

    result_data = await pts_client.pts_graphql_query(query)
    item = result_data.get("workOrderByID")

    if not item:
        raise RuntimeError(f"PTS 工单 {pts_order_id} 未找到")

    delivery = item.get("delivery") or {}
    project = delivery.get("project") or {}
    assigner = delivery.get("assigner") or {}
    company = item.get("company") or {}

    return {
        "company_name": company.get("name", ""),
        "delivery_id": delivery.get("id", ""),
        "project_id": project.get("id", ""),
        "project_name": project.get("name", ""),
        "assigner_name": assigner.get("name", ""),
        "desc": item.get("desc", ""),
    }


def resolve_delivery_info(assigner_name: str) -> dict:
    """Resolve delivery assigner → department → region leader.

    Returns: {delivery_assigner, department, region_leader}
    """
    department = ASSIGNER_DEPT.get(assigner_name, "")
    region_leader = DEPT_LEADER.get(department, "")

    if not department:
        raise RuntimeError(
            f"未识别到交付分配人: \"{assigner_name or '(空)'}\"，无法确定区域负责人"
        )
    if not region_leader:
        raise RuntimeError(f"部门 \"{department}\" 未配置区域负责人映射")

    return {
        "delivery_assigner": assigner_name,
        "department": department,
        "region_leader": region_leader,
    }


# ── Yunji API Flow ────────────────────────────────────────────────────────


def _parse_pts_order_id(input_str: str) -> str:
    """Extract 24-char hex PTS order ID from URL or raw ID."""
    import re
    m = re.search(r"([a-f0-9]{24})", input_str)
    return m.group(1) if m else input_str


def _date_to_timestamp(date_str: str) -> int:
    """Convert date string (YYYY-MM-DD) to millisecond timestamp."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return int(d.timestamp() * 1000)


async def create_yunji_requirement(
    pts_order_id: str,
    supplier_short_name: str,
    db=None,
) -> dict:
    """Full yunji dispatch flow: PTS data → yunji API → create requirement.

    Args:
        pts_order_id: PTS work order ID (24-char hex)
        supplier_short_name: e.g. "腾云", "平云"
        db: Optional SQLAlchemy session for local DB lookup

    Returns: {demandId, orderId, projectName, supplierName, ...}
    """
    order_id = _parse_pts_order_id(pts_order_id)
    short_name = _parse_supplier_short_name(supplier_short_name)
    supplier_full_name = SUPPLIER_MAP.get(short_name, supplier_short_name)

    logger.info("开始派单: 工单=%s, 供应商=%s", short_name, supplier_full_name)

    # 1. Fetch PTS data via GraphQL
    logger.info("[1/3] 读取 PTS 工单信息...")
    pts_info = await fetch_pts_info(order_id, db=db)
    logger.info("企业=%s, 项目ID=%s, 交付ID=%s, 分配人=%s",
                pts_info["company_name"], pts_info["project_id"],
                pts_info["delivery_id"], pts_info["assigner_name"])

    if not pts_info["project_id"]:
        raise RuntimeError("未能获取项目 ID，请检查工单是否关联了交付项目")

    # 2. Resolve delivery info
    logger.info("[2/3] 解析交付分配人...")
    delivery_info = resolve_delivery_info(pts_info["assigner_name"])
    logger.info("分配人: %s (%s) → 区域负责人: %s",
                delivery_info["delivery_assigner"],
                delivery_info["department"],
                delivery_info["region_leader"])

    # 3. Yunji API: create requirement
    logger.info("[3/3] 云集创建需求...")

    crm_project_id = pts_info["project_id"]

    # 3a. Parallel: create cart item + fetch CRM project info + partners + users
    cart_item, crm_project, products, partners, commissioners, regional_managers = await asyncio.gather(
        yunji_client.yunji_api("POST", "/api/admin/requirement-shopping-cart/create", {
            "serviceCat1": "产品服务", "serviceCat2": "无", "serviceUnit": "人天",
            "serviceMode": "现场", "userLevel": "L1", "cityCodeArr": ["000000"],
        }),
        yunji_client.yunji_api("GET", f"/api/admin/requirement/crm_project_info?crmId={crm_project_id}"),
        yunji_client.yunji_api("GET", f"/api/admin/requirement/project_products?crmProjectId={crm_project_id}"),
        yunji_client.yunji_api("GET", "/api/admin/partner/select_options"),
        yunji_client.yunji_api("GET", "/api/admin/user/commissioner_list"),
        yunji_client.yunji_api("GET", "/api/admin/user/regional_manager_list"),
    )
    if cart_item is None:
        raise RuntimeError("云集购物车创建返回空结果，可能Session已过期")
    if crm_project is None:
        raise RuntimeError("云集CRM项目信息返回空结果")

    project_name = crm_project.get("name", pts_info["project_name"])
    logger.info("项目: %s, 购物车ID: %s", project_name, cart_item.get("id"))

    # Find CRM product
    crm_product = None
    if products and len(products) > 0:
        crm_product = products[0]
        logger.info("商品: %s - %s", crm_product.get("productName"), crm_product.get("formName"))

    # Find supplier partner ID
    partner = _find_partner(partners, supplier_full_name)
    logger.info("供应商: %s (ID: %s)", partner["label"], partner["value"])

    # Find commissioner
    commissioner = _find_by_nickname(commissioners, DEFAULTS["outsource_specialist"])
    if not commissioner:
        raise RuntimeError(f"未找到外包专员: {DEFAULTS['outsource_specialist']}")

    # Find regional manager
    regional_manager = _find_by_nickname(regional_managers, delivery_info["region_leader"])
    if not regional_manager:
        raise RuntimeError(f"未找到区域负责人: {delivery_info['region_leader']}")

    logger.info("专员: %s, 负责人: %s", commissioner["nickname"], regional_manager["nickname"])

    # Build item data
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_ts = _date_to_timestamp(today_str)
    unit_price = DEFAULTS["unit_price"]

    item_data = {
        "cityCodeArr": ["000000"],
        "createTime": cart_item.get("createTime"),
        "id": cart_item.get("id"),
        "serviceCat1": "产品服务", "serviceCat2": "无",
        "serviceCatName1": "产品服务", "serviceCatName2": "无",
        "serviceMode": "现场", "serviceUnit": "人天",
        "userId": cart_item.get("userId"), "userLevel": "L1",
        "employeePlanList": [{
            "level": "L1",
            "userCount": DEFAULTS["headcount"],
            "userDayCount": DEFAULTS["total_man_days"],
            "price": unit_price,
        }],
        "isNeedClassScheduleVisible": False,
        "isNeedClassSchedule": False,
        "userCountVisible": True,
        "resultVisible": False,
        "budget": 0,
        "grossMargin": 0,
    }
    if crm_product:
        item_data["crmProduct"] = crm_product
    item_data["assignPartnerId"] = partner["value"]
    item_data["assignScene"] = DEFAULTS["designated_scenario"]
    item_data["outsourcingMode"] = DEFAULTS["outsource_method"]
    item_data["serviceBeginTime"] = today_ts
    item_data["serviceEndTime"] = today_ts
    item_data["deliveryContent"] = DEFAULTS["delivery_content"]
    item_data["deliveryItem"] = DEFAULTS["deliverable"]

    common_body = {
        "items": [item_data],
        "isAssignPartner": True, "totalBudget": 0, "lowPrice": False,
        "crmId": crm_project_id, "projectName": project_name,
        "commissioner": commissioner["username"],
        "regionalManager": regional_manager["username"],
    }

    # Calculate budget
    logger.info("计算预算并创建需求...")
    budget_result = await yunji_client.yunji_api(
        "POST", "/api/admin/requirement/calc_budget", common_body
    )
    if budget_result is None:
        raise RuntimeError("云集计算预算API返回空结果，可能Session已过期")

    item_data["budget"] = budget_result.get("totalBudget", 0)
    item_data["grossMargin"] = (budget_result.get("grossMargins") or [0])[0]
    item_data["isNeedInterview"] = DEFAULTS["need_interview"] == "是"
    item_data["travelBudget"] = DEFAULTS["travel_budget"]
    item_data["remark"] = project_name

    common_body["totalBudget"] = budget_result.get("totalBudget", 0)
    common_body["lowPrice"] = budget_result.get("lowPrice", False)

    # Create requirement
    result = await yunji_client.yunji_api(
        "POST", "/api/admin/requirement/create", common_body
    )
    if result is None:
        raise RuntimeError("云集创建需求API返回空结果，可能Session已过期或请求被拒绝")
    demand_id = str(result.get("id", ""))
    logger.info("需求创建成功! ID=%s", demand_id)

    # Get order ID
    order_id_result = ""
    try:
        orders = await yunji_client.yunji_api(
            "GET", f"/api/admin/requirement-order/partner_orders?requirementId={demand_id}&batch=1"
        )
        if orders and len(orders) > 0:
            order_id_result = str(orders[0].get("orderId", ""))
            logger.info("订单编号: %s", order_id_result)
    except Exception as e:
        logger.warning("获取订单编号失败: %s", e)

    # Cleanup cart (best-effort, don't wait)
    try:
        asyncio.create_task(_cleanup_cart(cart_item.get("id")))
    except Exception:
        pass

    return {
        "demandId": demand_id,
        "orderId": order_id_result,
        "projectName": result.get("projectName", project_name),
        "commissionerName": result.get("commissionerName", ""),
        "regionalManagerName": result.get("regionalManagerName", ""),
        "totalBudget": result.get("totalBudget", 0),
        "supplierName": f"{short_name} ({partner.get('label', supplier_full_name)})",
        "crmId": crm_project_id,
    }


# ── Helpers ───────────────────────────────────────────────────────────────


def _find_partner(partners: list, supplier_full_name: str) -> dict:
    """Find partner by name, with fuzzy matching fallback."""
    if not isinstance(partners, list):
        raise RuntimeError(f"云集供应商列表返回格式异常: {type(partners).__name__}")

    # Exact match
    for p in partners:
        label = p.get("label", "")
        if label == supplier_full_name:
            return p

    # Fuzzy match
    for p in partners:
        label = p.get("label", "")
        if supplier_full_name in label or label in supplier_full_name:
            return p

    raise RuntimeError(f"未找到供应商: {supplier_full_name}，请检查供应商名称")


def _find_by_nickname(users: list, nickname: str) -> dict | None:
    """Find user by nickname in yunji user list."""
    if not isinstance(users, list):
        raise RuntimeError(f"云集用户列表返回格式异常: {type(users).__name__}")
    for u in users:
        if u.get("nickname") == nickname:
            return u
    return None


async def _cleanup_cart(cart_id: str) -> None:
    """Delete shopping cart item (best-effort)."""
    try:
        await yunji_client.yunji_api(
            "POST", "/api/admin/requirement-shopping-cart/delete",
            {"ids": [str(cart_id)]},
        )
    except Exception:
        pass  # Best-effort cleanup
