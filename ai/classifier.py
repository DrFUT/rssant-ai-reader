"""智能分类。

重要设计取舍:
    分类**不单独发起 LLM 调用**。摘要任务已经让模型返回了 topic + keywords,
    这里只做:
        1. 标签归一化(去掉噪声词、统一大小写、限制数量)
        2. 主题校验/兜底 —— 模型返回了不在枚举里的主题时, 用关键词规则兜底
        3. 完全无 LLM 时(没配 Key 或调用失败)退化成纯规则分类

    这样在 2 核 2GB 的机器上, 每篇文章只消耗一次 LLM 调用。
"""

import logging
import re
from typing import List, Optional, Tuple

from .prompts import DEFAULT_TOPIC, TOPICS
from .text import contains

LOG = logging.getLogger(__name__)

# 主题关键词规则表(兜底用, 顺序即优先级)
TOPIC_RULES = [
    (
        'AI',
        (
            'ai', '人工智能', '大模型', 'llm', 'gpt', 'chatgpt', 'deepseek', 'claude',
            'gemini', 'openai', 'anthropic', '机器学习', '深度学习', '神经网络',
            'transformer', 'agent', '智能体', 'aigc', '生成式', 'stable diffusion',
            'rag', '微调', '推理模型', '多模态',
        ),
    ),
    (
        '编程',
        (
            'python', 'rust', 'golang', 'go 语言', 'java', 'javascript', 'typescript',
            '代码', '编程', '开源', 'github', '框架', '库', 'api 设计', '重构',
            '编译器', '算法', '数据结构', 'linux', '内核', '数据库', 'sql',
            'docker', 'kubernetes', 'git', 'django', 'flask', 'fastapi', 'react',
            'vue', 'node', 'spring', 'redis', 'postgres',
        ),
    ),
    (
        '技术',
        (
            '技术', '架构', '系统', '服务器', '云原生', '安全', '漏洞', '浏览器',
            '芯片', '半导体', '硬件', '网络', '协议', '性能', '运维', 'devops',
            '前端', '后端', '移动端', 'android', 'ios', '操作系统',
        ),
    ),
    (
        '商业',
        (
            '商业', '融资', '上市', 'ipo', '收购', '并购', '营收', '财报', '估值',
            '市场', '经济', '创业', '公司', '股价', '投资', '基金', '裁员',
            '营销', '电商', '供应链', '政策', '监管',
        ),
    ),
    (
        '科学',
        (
            '科学', '研究', '论文', '物理', '化学', '生物', '医学', '天文', '航天',
            '量子', '材料', '能源', '气候', '实验', '发现', 'nature', 'science',
        ),
    ),
    (
        '娱乐',
        (
            '电影', '电视', '剧集', '音乐', '游戏', '综艺', '明星', '动漫', '小说',
            '漫画', '娱乐', '体育', '足球', '篮球', '赛事', '直播', '网红',
            '欧冠', '英超', 'nba', '夺冠', '联赛', '票房', '演唱会', '新番',
        ),
    ),
]

# 对分类没有帮助的噪声词
STOPWORDS = {
    '文章', '本文', '作者', '分享', '转载', '阅读', '原文', '推荐', '点击',
    '评论', '订阅', '来源', '新闻', '日报', '周刊', '最新', '今日', '内容',
}


def normalize_tags(tags, limit: int = 6, max_len: int = 20) -> List[str]:
    """标签归一化: 去噪声、去重、限制长度与数量。"""
    result = []
    if isinstance(tags, str):
        tags = re.split(r'[,，、;；|/]\s*', tags)
    if not isinstance(tags, (list, tuple)):
        return result
    for item in tags:
        if item is None:
            continue
        text = str(item).strip().strip('#').strip()
        if not text:
            continue
        text = re.sub(r'\s+', ' ', text)
        if len(text) > max_len:
            text = text[:max_len]
        low = text.lower()
        if low in STOPWORDS:
            continue
        # 大小写/中英文去重
        exists = False
        for kept in result:
            if kept.lower() == low:
                exists = True
                break
        if exists:
            continue
        result.append(text)
        if len(result) >= limit:
            break
    return result


def normalize_topic(topic) -> str:
    """把模型返回的 topic 收敛到枚举集合内。"""
    if topic:
        text = str(topic).strip()
        for item in TOPICS:
            if text == item or text.lower() == item.lower():
                return item
        # 模糊匹配: 模型可能返回 "人工智能" 这种近义词
        for item in TOPICS:
            if item in text:
                return item
    return DEFAULT_TOPIC


def classify_by_rules(title: str = None, text: str = None) -> str:
    """纯规则分类, 用于无 LLM 或 LLM 返回不可信时的兜底。"""
    haystack = f'{title or ""}\n{text or ""}'
    if not haystack.strip():
        return DEFAULT_TOPIC
    best_topic, best_hits = DEFAULT_TOPIC, 0
    for topic, keywords in TOPIC_RULES:
        hits = 0
        for keyword in keywords:
            if contains(haystack, keyword):
                hits += 1
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic


def classify(
    topic=None,
    keywords=None,
    title: str = None,
    text: str = None,
) -> Tuple[str, List[str]]:
    """产出最终的主题与标签。

    返回 (topic, tags)。
    """
    tags = normalize_tags(keywords)
    normalized = normalize_topic(topic)
    if normalized == DEFAULT_TOPIC:
        # 模型没给出可信主题, 用规则补一次
        rule_topic = classify_by_rules(title=title, text=text)
        if rule_topic != DEFAULT_TOPIC:
            normalized = rule_topic
        elif tags:
            # 再用标签本身试一次
            normalized = classify_by_rules(title=' '.join(tags))
    return normalized, tags


def topic_of(tags: Optional[List[str]]) -> str:
    """只根据标签猜主题。"""
    return classify_by_rules(title=' '.join(tags or []))
