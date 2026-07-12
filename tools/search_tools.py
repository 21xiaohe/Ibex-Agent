"""tools/search_tools.py —— Ibex 的检索工具

把"联网搜索 / 取网页内容"封装成工具。这里给出最简形态（你可换成 Tavily/Exa 等）。
I/O 型工具用 async，避免阻塞事件循环（本课异步基调）。
"""
from langchain.tools import tool


@tool
async def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索，返回与 query 最相关的若干结果摘要。
    用于需要查阅外部最新信息（如某库的最新用法、报错原因）时。"""
    # 教学占位：真实实现接入 Tavily/Exa/SerpAPI 等，并用异步 HTTP 客户端请求。
    # from tavily import AsyncTavilyClient ...
    return f"[web_search 占位] 这里应返回关于「{query}」的前 {max_results} 条结果。"


@tool
async def fetch_url(url: str) -> str:
    """抓取给定 URL 的网页正文，用于阅读某个具体文档/页面。"""
    # 教学占位：真实实现用 httpx.AsyncClient 抓取并抽取正文。
    return f"[fetch_url 占位] 这里应返回 {url} 的正文内容。"