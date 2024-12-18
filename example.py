import asyncio
import aiohttp
from cf_turnstile_bypass import TurnstileSolver, TurnstileConfig
from loguru import logger

async def main():
    # 自定义配置（可选）
    config = TurnstileConfig(
        chrome_path='C:\Program Files\Google\Chrome\Application\chrome.exe',
        max_attempts=5,
        screencast_video_path=None,
        initial_wait_time=0.6,
        proxy="http://127.0.0.1:7890",
    )

    solver = TurnstileSolver(logger, config)
    
    try:
        headers = await solver.solve(
            url="https://test.aiuuo.com",
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        print("验证成功，获取到的headers:", headers)
        
        # 添加验证请求
        async with aiohttp.ClientSession() as session:
            async with session.get("https://test.aiuuo.com", headers=headers) as response:
                if response.status == 403:
                    print("验证失败：仍然遇到 Cloudflare Turnstile 验证")
                else:
                    print(f"验证成功！状态码: {response.status}")
                    content = await response.text()
                    print("页面内容预览:", content[:200])
                    
    except Exception as e:
        print(f"验证失败: {e}")

if __name__ == "__main__":
    asyncio.run(main()) 