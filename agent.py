"""
管网监测 Agent - 基于 smolagents 框架
使用 ToolCallingAgent 实现按需数据获取和简洁回复
"""
import logging
import json
import traceback
from smolagents import ToolCallingAgent, OpenAIModel

import config_secrets
from tools import ALL_TOOLS

logger = logging.getLogger(__name__)

AGENT_SYSTEM_PROMPT = """你是管网监测助手。回复必须简短（50-100字）。

## 规则
1. 用户要图表 → 调 generate_chart 工具，回复一句话说明
2. 用户问设备 → 调 get_device_readings，回复当前液位+是否正常
3. 用户问天气 → 调 get_weather，回复降雨量
4. 正常现象直接说"正常"，不要解释原因
5. 不要列点、不要分段、不要科普

## 示例
用户：HSTX_TSPS_TX11 液位多少
正确：当前液位1.3m，正常范围。
错误：设备HSTX_TSPS_TX11当前液位为1.313m，在正常范围（0.1-2.0m）内...

用户：给我展示折线图
正确：已生成折线图（调用generate_chart后发图）
错误：液位折线图是用连续线条显示液位随时间变化的图表...

城西管网：液位正常0.1-2.0m，>2.5m高风险
"""


def create_agent():
    """创建 smolagents ToolCallingAgent"""
    model = OpenAIModel(
        model_id=config_secrets.MIMO_MODEL,
        api_base=config_secrets.MIMO_API_BASE,
        api_key=config_secrets.MIMO_API_KEY,
    )

    from smolagents.agents import EMPTY_PROMPT_TEMPLATES
    prompt_templates = {**EMPTY_PROMPT_TEMPLATES, "system_prompt": AGENT_SYSTEM_PROMPT}

    agent = ToolCallingAgent(
        tools=ALL_TOOLS,
        model=model,
        max_steps=5,
        prompt_templates=prompt_templates,
        verbosity_level=0,
    )
    return agent


def run_agent(user_message: str) -> tuple:
    """
    运行 agent 处理用户消息
    返回: (回复文本, 图表路径列表)
    """
    chart_paths = []

    try:
        logger.info("Creating agent...")
        agent = create_agent()
        logger.info("Running agent with message: %s", user_message[:50])
        result = agent.run(user_message)
        logger.info("Agent result: %s", result[:100] if result else None)

        # 从 agent 的 memory 中提取生成的图表路径
        for step in agent.memory.steps:
            if hasattr(step, 'tool_output'):
                try:
                    output = json.loads(step.tool_output)
                    if output.get("chart_path"):
                        chart_paths.append(output["chart_path"])
                except (json.JSONDecodeError, TypeError):
                    pass

        # 从回复文本中提取图表路径（备用方案）
        if not chart_paths and result:
            import re
            paths = re.findall(r'[A-Za-z]:\\[^"\'`\s]+\.png|/[^"\'`\s]+\.png', result)
            chart_paths.extend(paths)

        return result or "分析完成", chart_paths

    except Exception as e:
        logger.error("Agent error: %s\n%s", e, traceback.format_exc())
        return None, []
