"""临时：直接探针 DashScope chat completions（非流式，看真实拒绝原因）。"""

import asyncio
import sys

sys.path.insert(0, "d:/zcode/实战项目1/backend")

from app.core.config import get_settings


async def main() -> None:
    settings = get_settings()
    import httpx

    url = f"{settings.dashscope_base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.dashscope_api_key.get_secret_value()}"}
    payload = {
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": "回复 OK 两个字"}],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        print("status:", resp.status_code)
        print("body:", resp.text[:400])


asyncio.run(main())
