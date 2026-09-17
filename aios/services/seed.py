"""First-launch seed data.

The eight modules below are the original ``TRACKS`` constant from
``aios_daily.py``, split into the finer topic granularity the new data model
supports. Every query string from the original script is preserved verbatim
(marked ``legacy`` in the comments) so collection behaviour does not regress;
the additional queries only narrow each topic further.

Seeding happens **once**, on an empty database. It never overwrites user
configuration on subsequent launches - that is the whole point of moving this
configuration out of Python.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ExcludedKeyword, MonitorModule, PreferredSource, SearchQuery, Topic

logger = logging.getLogger(__name__)


#: Keywords that almost always indicate noise rather than intelligence.
DEFAULT_EXCLUDED_KEYWORDS = ["招聘", "二手", "优惠券", "促销", "打折", "论坛转载", "刷机教程"]


SEED_MODULES: list[dict[str, Any]] = [
    {
        "key": "mobile",
        "name": "移动智能终端侧",
        "description": "手机与平板操作系统的系统级 AI、Agent 框架与生态进展。",
        "sort_order": 10,
        "preferred_sources": ["huawei.com", "developer.huawei.com", "consumer.huawei.com"],
        "topics": [
            {
                "name": "HarmonyOS",
                "description": "鸿蒙操作系统版本迭代、Agent 框架与原生应用生态。",
                "queries": [
                    # legacy: first query of the original "mobile" track
                    'HarmonyOS OR 鸿蒙 OR "Android 17" OR iOS "operating system" AI agent',
                    "HarmonyOS Agent Framework Kit",
                    "鸿蒙 原生应用 生态",
                    "小艺 Agent 智能体",
                ],
                "preferred_sources": ["huawei.com", "developer.huawei.com"],
            },
            {
                "name": "Android",
                "description": "Android 平台的系统级 AI 能力与端侧模型。",
                "queries": [
                    # legacy: second query of the original "mobile" track
                    '"mobile operating system" AI agent smartphone',
                    "Android AI system feature Gemini on-device",
                    "Android XR OR Android 17 platform release",
                ],
                "preferred_sources": ["android.com", "google.com", "blog.google"],
            },
            {
                "name": "Apple Intelligence",
                "description": "iOS / iPadOS 的端侧智能与 App Intents 生态。",
                "queries": [
                    "Apple Intelligence iOS on-device model",
                    "iOS App Intents Siri assistant update",
                ],
                "preferred_sources": ["apple.com", "developer.apple.com"],
            },
        ],
    },
    {
        "key": "pc",
        "name": "PC 侧",
        "description": "桌面操作系统的 Agent 化、AI PC 硬件与系统协同。",
        "sort_order": 20,
        "preferred_sources": ["microsoft.com", "huawei.com"],
        "topics": [
            {
                "name": "Windows",
                "description": "Windows 的 Agent 化能力与开发者接口。",
                "queries": [
                    # legacy: first query of the original "pc" track
                    'Windows agentic OS OR "agent-ready" PC',
                    "Windows Copilot runtime agent API",
                ],
                "preferred_sources": ["microsoft.com", "blogs.windows.com"],
            },
            {
                "name": "HarmonyOS PC",
                "description": "鸿蒙电脑的系统能力与生态适配。",
                "queries": [
                    # legacy: second query of the original "pc" track
                    'HarmonyOS PC OR 鸿蒙电脑 OR "AI PC" operating system',
                    "鸿蒙电脑 办公应用 适配",
                ],
                "preferred_sources": ["huawei.com", "consumer.huawei.com"],
            },
            {
                "name": "AI PC",
                "description": "NPU 平台与端侧推理能力在 PC 上的落地。",
                "queries": [
                    "AI PC NPU on-device inference laptop",
                    "Copilot+ PC Snapdragon X OR Lunar Lake NPU",
                ],
                "preferred_sources": ["intel.com", "qualcomm.com", "amd.com"],
            },
        ],
    },
    {
        "key": "server",
        "name": "服务器侧",
        "description": "服务器操作系统与 AI 基础软件栈。",
        "sort_order": 30,
        "preferred_sources": ["openeuler.org", "openanolis.cn", "openatom.org"],
        "topics": [
            {
                "name": "openEuler",
                "description": "openEuler 版本、内核特性与 AI 场景能力。",
                "queries": [
                    # legacy: first query of the original "server" track
                    "openEuler OR Anolis OS OR 龙蜥 operating system server",
                    "openEuler LTS release kernel AI",
                ],
                "preferred_sources": ["openeuler.org", "gitee.com"],
            },
            {
                "name": "Anolis",
                "description": "龙蜥操作系统与其社区生态。",
                "queries": [
                    "Anolis OS 龙蜥 社区 版本发布",
                    "龙蜥 操作系统 迁移 兼容",
                ],
                "preferred_sources": ["openanolis.cn"],
            },
            {
                "name": "Linux AI",
                "description": "服务器 Linux 上的 AI 运行时与调度栈。",
                "queries": [
                    # legacy: second query of the original "server" track
                    '"server operating system" AI Linux China',
                    "Linux kernel AI scheduler GPU runtime",
                ],
                "preferred_sources": ["kernel.org", "redhat.com"],
            },
        ],
    },
    {
        "key": "supernode",
        "name": "智算超节点侧",
        "description": "超节点架构、互联总线与大规模训练集群。",
        "sort_order": 40,
        "preferred_sources": ["huawei.com", "nvidia.com"],
        "topics": [
            {
                "name": "Huawei SuperPoD",
                "description": "昇腾超节点产品与集群部署。",
                "queries": [
                    # legacy: second query of the original "supernode" track
                    "Atlas 950 SuperPoD OR AI supernode",
                    "昇腾 超节点 集群 训练",
                ],
                "preferred_sources": ["huawei.com"],
            },
            {
                "name": "国产超节点",
                "description": "国内厂商的超节点与智算中心方案。",
                "queries": [
                    "国产 超节点 智算中心 发布",
                    "智算集群 万卡 算力 建设",
                ],
            },
            {
                "name": "Interconnect",
                "description": "高速互联总线与 scale-up 网络技术。",
                "queries": [
                    # legacy: first query of the original "supernode" track
                    "SuperPoD OR 超节点 AI cluster interconnect",
                    "NVLink OR UALink OR 灵衢 interconnect bus",
                ],
                "preferred_sources": ["nvidia.com"],
            },
        ],
    },
    {
        "key": "iot",
        "name": "物联网侧",
        "description": "物联网与嵌入式操作系统的智能化。",
        "sort_order": 50,
        "preferred_sources": ["openatom.org", "openharmony.cn"],
        "topics": [
            {
                "name": "OpenHarmony",
                "description": "开源鸿蒙在设备侧的发行版与认证。",
                "queries": [
                    # legacy: first query of the original "iot" track
                    "OpenHarmony IoT OR 开源鸿蒙 物联网",
                    "OpenHarmony 发行版 兼容性测评",
                ],
                "preferred_sources": ["openatom.org", "gitee.com"],
            },
            {
                "name": "RTOS",
                "description": "实时操作系统与 RISC-V 生态。",
                "queries": [
                    # legacy: second query of the original "iot" track
                    "RISC-V OpenHarmony industrial IoT",
                    "RT-Thread OR FreeRTOS OR Zephyr release",
                ],
                "preferred_sources": ["rt-thread.org", "zephyrproject.org"],
            },
            {
                "name": "Industrial IoT",
                "description": "工业物联网平台与边缘智能。",
                "queries": [
                    "industrial IoT edge AI platform 工业互联网",
                    "边缘计算 操作系统 工业 部署",
                ],
            },
        ],
    },
    {
        "key": "uav",
        "name": "无人飞行器侧",
        "description": "无人机飞控系统、机载智能与低空经济。",
        "sort_order": 60,
        "topics": [
            {
                "name": "Flight Control OS",
                "description": "飞控操作系统与自动驾驶栈。",
                "queries": [
                    # legacy: first query of the original "uav" track
                    "drone operating system AI flight controller RT-Thread",
                    "PX4 OR ArduPilot flight stack release",
                ],
            },
            {
                "name": "Edge AI UAV",
                "description": "机载边缘计算与感知模型。",
                "queries": [
                    # legacy: second query of the original "uav" track
                    "无人机 操作系统 AI 飞控",
                    "drone onboard edge AI perception compute",
                ],
            },
            {
                "name": "Low Altitude Economy",
                "description": "低空经济政策、空域管理与运营系统。",
                "queries": [
                    "低空经济 空域 管理 平台",
                    "eVTOL certification airspace management system",
                ],
            },
        ],
    },
    {
        "key": "robotics",
        "name": "具身智能侧",
        "description": "机器人操作系统与具身智能模型栈。",
        "sort_order": 70,
        "preferred_sources": ["ros.org", "nvidia.com"],
        "topics": [
            {
                "name": "ROS",
                "description": "ROS 2 发行版与机器人中间件。",
                "queries": [
                    # legacy: first query of the original "robotics" track
                    "robot operating system embodied AI ROS 2",
                    "ROS 2 distribution release middleware",
                ],
                "preferred_sources": ["ros.org"],
            },
            {
                "name": "OpenHarmony Robotics",
                "description": "鸿蒙系机器人操作系统与整机方案。",
                "queries": [
                    # legacy: second query of the original "robotics" track
                    "M-Robots OS OpenHarmony robot",
                    "人形机器人 操作系统 开源鸿蒙",
                ],
            },
            {
                "name": "Isaac / VLA",
                "description": "视觉-语言-动作模型与机器人仿真平台。",
                "queries": [
                    "NVIDIA Isaac GR00T humanoid robot platform",
                    "vision language action model robot VLA",
                ],
                "preferred_sources": ["nvidia.com"],
            },
        ],
    },
    {
        "key": "space",
        "name": "太空智算侧",
        "description": "在轨计算、卫星操作系统与空间 AI。",
        "sort_order": 80,
        "topics": [
            {
                "name": "Space OS",
                "description": "航天器与卫星的操作系统。",
                "queries": [
                    # legacy: second query of the original "space" track
                    "太空算力 太空操作系统 卫星",
                    "spacecraft operating system real-time onboard",
                ],
            },
            {
                "name": "Satellite Computing",
                "description": "在轨算力星座与星上数据处理。",
                "queries": [
                    # legacy: first query of the original "space" track
                    "space computing operating system satellite AI",
                    "在轨 计算 星座 算力 卫星",
                ],
            },
            {
                "name": "Space AI",
                "description": "空间 AI 模型部署与星地协同。",
                "queries": [
                    "satellite onboard AI inference model",
                    "星地 协同 人工智能 遥感",
                ],
            },
        ],
    },
]


DEFAULT_MODULE_PROMPT = ""
DEFAULT_TOPIC_PROMPT = ""


def is_empty(session: Session) -> bool:
    """True when no monitoring configuration exists yet."""
    return (session.scalar(select(func.count()).select_from(MonitorModule)) or 0) == 0


def seed_if_empty(session: Session, force: bool = False) -> bool:
    """Populate default modules/topics/queries on an empty database.

    Returns True when seeding actually ran. Existing user configuration is never
    touched unless ``force`` is set (used only by tests and fixtures).
    """
    if not force and not is_empty(session):
        return False

    for module_spec in SEED_MODULES:
        module = MonitorModule(
            key=module_spec["key"],
            name=module_spec["name"],
            description=module_spec.get("description", ""),
            enabled=True,
            sort_order=module_spec.get("sort_order", 0),
            lookback_days=3,
            max_candidates=18,
            max_report_items=2,
            analysis_prompt=module_spec.get("analysis_prompt", DEFAULT_MODULE_PROMPT),
        )
        session.add(module)
        session.flush()

        for domain in module_spec.get("preferred_sources", []):
            session.add(
                PreferredSource(module_id=module.id, domain=domain.lower(), priority=5)
            )

        for topic_order, topic_spec in enumerate(module_spec.get("topics", []), start=1):
            topic = Topic(
                module_id=module.id,
                name=topic_spec["name"],
                description=topic_spec.get("description", ""),
                enabled=True,
                sort_order=topic_order * 10,
                analysis_prompt=topic_spec.get("analysis_prompt", DEFAULT_TOPIC_PROMPT),
            )
            session.add(topic)
            session.flush()

            for query_order, query in enumerate(topic_spec.get("queries", [])):
                session.add(
                    SearchQuery(
                        topic_id=topic.id,
                        query=query,
                        enabled=True,
                        # Earlier queries in the list run first.
                        priority=max(0, 10 - query_order),
                    )
                )

            for domain in topic_spec.get("preferred_sources", []):
                session.add(
                    PreferredSource(topic_id=topic.id, domain=domain.lower(), priority=8)
                )

    for keyword in DEFAULT_EXCLUDED_KEYWORDS:
        session.add(ExcludedKeyword(keyword=keyword))

    session.flush()
    logger.info("Seeded %s default monitor modules", len(SEED_MODULES))
    return True


def seed_summary() -> dict[str, int]:
    """Counts of what a fresh seed creates - used by the startup banner."""
    topics = sum(len(m.get("topics", [])) for m in SEED_MODULES)
    queries = sum(
        len(t.get("queries", [])) for m in SEED_MODULES for t in m.get("topics", [])
    )
    return {"modules": len(SEED_MODULES), "topics": topics, "queries": queries}
