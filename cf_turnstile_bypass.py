from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
import asyncio
import json
import loguru
from pathlib import Path
from datetime import datetime
from DrissionPage import ChromiumPage, ChromiumOptions
from enum import Enum
import sys
import re
from urllib.parse import urlparse
from asyncio import Semaphore

class LoggingMode(Enum):
    """日志记录模式"""
    DISABLED = "disabled"  # 关闭日志
    CONSOLE = "console"    # 仅控制台输出
    FILE = "file"         # 控制台输出并写入文件

@dataclass
class TurnstileConfig:
    """Turnstile 验证配置类"""
    # 浏览器配置
    chrome_path: str = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
    user_data_path: Optional[str] = None
    
    # 验证重试配置
    max_attempts: int = 10
    click_max_attempts: int = 5
    wait_time: float = 1.0
    verify_timeout: int = 10
    page_load_timeout: int = 30
    initial_wait_time: float = 1.0
    
    # 输出配置
    screencast_video_path: str = 'turnstile'
    headers_output_path: Optional[str] = None
    save_debug_screenshot: bool = False
    debug_screenshot_path: str = 'debug_screenshots'
    
    # 浏览器启动参数
    browser_arguments: List[str] = field(default_factory=lambda: [
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--disable-software-rasterizer',
    ])
    
    # Headers配置
    default_headers: Dict[str, str] = field(default_factory=lambda: {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "sec-ch-ua": '"Chromium";v="112", "Google Chrome";v="112", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    
    # 日志配置
    logging_mode: LoggingMode = LoggingMode.CONSOLE
    log_file_path: str = "turnstile.log"
    
    # 代理配置
    proxy: Optional[str] = None  # 支持 socks5://user:pass@host:port 或 http://user:pass@host:port
    max_concurrent_tasks: int = 3  # 最大并发任务数
    cache_timeout: int = 300  # headers缓存超时时间（秒）
    
    def __post_init__(self):
        """配置后处理，确保路径存在并初始化日志"""
        if self.screencast_video_path and self.screencast_video_path.strip():
            Path(self.screencast_video_path).mkdir(parents=True, exist_ok=True)
        if self.save_debug_screenshot:
            Path(self.debug_screenshot_path).mkdir(parents=True, exist_ok=True)
        
        # 确保日志文件目录存在
        if self.logging_mode == LoggingMode.FILE:
            Path(self.log_file_path).parent.mkdir(parents=True, exist_ok=True)

class TurnstileError(Exception):
    """Turnstile 验证相关错误的基类"""
    pass

class TurnstileTimeoutError(TurnstileError):
    """超时错误"""
    pass

class TurnstileVerificationError(TurnstileError):
    """验证失败错误"""
    pass

class TurnstileSolver:
    """Turnstile Turnstile 验证解决器"""
    
    VERIFY_TEXTS = [
        'Verify you are human',
        '确认您是真人',
        '确认您是人类',
        'Verify that you are human',
        '请验证您是人类',
    ]

    # 添加类变量用于缓存控制
    _cache: Dict[str, Dict[str, Any]] = {}  # {cache_key: {'headers': headers, 'timestamp': timestamp}}
    _locks: Dict[str, asyncio.Lock] = {}    # {cache_key: lock}
    _semaphore: Optional[Semaphore] = None  # 并发控制信号量
    
    def __init__(self, logger: Optional['loguru.Logger'] = None, config: Optional[TurnstileConfig] = None):
        self.config = config or TurnstileConfig()
        self.logger = self._setup_logger(logger)
        self._page: Optional[ChromiumPage] = None
        self._verification_start_time: Optional[datetime] = None
        self._last_error: Optional[Exception] = None
        self._status: str = "initialized"
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_tasks)

    def _setup_logger(self, logger: Optional['loguru.Logger']) -> 'loguru.Logger':
        """设置日志记录器"""
        if logger is None:
            logger = loguru.logger
        logger.remove()
        
        if self.config.logging_mode != LoggingMode.DISABLED:
            logger.add(
                sys.stderr,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
                level="DEBUG"
            )
            if self.config.logging_mode == LoggingMode.FILE:
                logger.add(
                    self.config.log_file_path,
                    rotation="500 MB",
                    retention="10 days",
                    compression="zip",
                    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
                    level="DEBUG"
                )
        
        return logger

    def _log(self, level: str, message: str):
        """统一的日志记录方法"""
        if self.config.logging_mode != LoggingMode.DISABLED:
            getattr(self.logger, level)(message)

    def _init_browser_options(self, user_agent: str) -> ChromiumOptions:
        """初始化浏览器选项"""
        options = (
            ChromiumOptions()
            .auto_port()
            .set_browser_path(self.config.chrome_path)
            .headless()
            .incognito(True)
            .set_user_agent(user_agent)
            .set_argument('--guest')
        )
        
        # 添加代理配置
        if self.config.proxy:
            options.set_argument(f'--proxy-server={self.config.proxy}')
        
        for arg in self.config.browser_arguments:
            options.set_argument(arg)
            
        if self.config.user_data_path:
            options.set_user_data_path(self.config.user_data_path)
            
        return options

    async def _save_debug_screenshot(self, name: str):
        """保存调试截图"""
        if self.config.save_debug_screenshot and self._page:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{timestamp}_{name}.png"
            path = Path(self.config.debug_screenshot_path) / filename
            self._page.save_screenshot(path, full_page=True)
            self.logger.debug(f"保存调试截图: {path}")

    async def _handle_verification(self) -> bool:
        """处理验证过程，返回是否验证成功"""
        self._status = "verifying"
        try:
            divs = self._page.eles('tag:div', timeout=15)
            iframe = None
            for div in divs:
                if div.shadow_root:
                    iframe = div.shadow_root.ele(
                        "xpath://iframe[starts-with(@src, 'https://challenges.cloudflare.com/')]",
                        timeout=0
                    )
                    if iframe:
                        break
                        
            if not iframe:
                self._log('info', '未检测到验证码挑战，验证完成')
                return True

            await self._save_debug_screenshot("before_verification")
            
            body_element = iframe.ele('tag:body', timeout=15).shadow_root
            verify_element = body_element.ele(self._generate_verify_xpath(), timeout=5)
            
            if not verify_element:
                await self._save_debug_screenshot("no_verify_button")
                raise TurnstileVerificationError("无法找到验证按钮")

            # 点击验证按钮
            for click_attempt in range(self.config.click_max_attempts):
                try:
                    verify_element.click()
                    self._log('debug', f'验证按钮点击成功 (尝试 {click_attempt + 1})')
                    break
                except Exception as e:
                    await self._save_debug_screenshot(f"click_failed_{click_attempt}")
                    if click_attempt == self.config.click_max_attempts - 1:
                        raise TurnstileVerificationError(f"验证按钮点击失败: {str(e)}")
                    await asyncio.sleep(self.config.wait_time)

            # 等待验证完成
            verify_element.wait.deleted(timeout=self.config.verify_timeout)
            await asyncio.sleep(self.config.wait_time)
            await self._save_debug_screenshot("after_verification")
            return True

        except Exception as e:
            self._last_error = e
            self._log('error', f"验证过程出错: {str(e)}")
            return False

    @classmethod
    def _generate_verify_xpath(cls) -> str:
        """生成验证按钮的 XPath"""
        conditions = [f"text()='{text}'" for text in cls.VERIFY_TEXTS]
        xpath_condition = " or ".join(conditions)
        return f"xpath://*[{xpath_condition}]"

    def _extract_headers(self, url: str, user_agent: str) -> Dict[str, str]:
        """提取并构建 headers"""
        cookies = self._page.cookies(all_domains=False, all_info=False)
        if not isinstance(cookies, list):
            raise TurnstileError(f"获取到的Cookies格式不正确: {type(cookies)}")

        cookie_str = '; '.join([
            f"{cookie['name']}={cookie['value']}" 
            for cookie in cookies 
            if 'name' in cookie and 'value' in cookie
        ])

        # 使用配置中的默认headers，并添加动态的headers
        headers = self.config.default_headers.copy()
        headers.update({
            "cookie": cookie_str,
            "referer": url,
            "user-agent": user_agent,
        })
        
        return headers

    @classmethod
    def _get_cache_key(cls, url: str, proxy_ip: Optional[str] = None) -> str:
        """生成缓存键"""
        hostname = urlparse(url).netloc
        return f"{hostname}:{proxy_ip if proxy_ip else 'direct'}"

    @classmethod
    def _get_proxy_ip(cls, proxy: Optional[str]) -> Optional[str]:
        """从代理URL中提取IP地址"""
        if not proxy:
            return None
        match = re.search(r'://(?:.*@)?([^:]+):', proxy)
        return match.group(1) if match else None

    async def solve(self, url: str, user_agent: str) -> Dict[str, str]:
        """解决 Cloudflare Turnstile 验证并返回 headers"""
        proxy_ip = self._get_proxy_ip(self.config.proxy)
        cache_key = self._get_cache_key(url, proxy_ip)
        
        # 检查缓存
        if cache_key in self._cache:
            cache_data = self._cache[cache_key]
            if (datetime.now() - cache_data['timestamp']).total_seconds() < self.config.cache_timeout:
                self._log('info', f"使用缓存的headers: {cache_key}")
                return cache_data['headers']
        
        # 获取或创建锁
        if cache_key not in self._locks:
            self._locks[cache_key] = asyncio.Lock()
        
        async with self._locks[cache_key]:
            # 二次检查缓存（防止竞争条件）
            if cache_key in self._cache:
                cache_data = self._cache[cache_key]
                if (datetime.now() - cache_data['timestamp']).total_seconds() < self.config.cache_timeout:
                    return cache_data['headers']
            
            # 使用信号量控制并发
            async with self._semaphore:
                headers = await self._solve_internal(url, user_agent)
                
                # 更新缓存
                self._cache[cache_key] = {
                    'headers': headers,
                    'timestamp': datetime.now()
                }
                
                return headers

    async def _solve_internal(self, url: str, user_agent: str) -> Dict[str, str]:
        """内部解决方法，包含原始的solve逻辑"""
        start_time = datetime.now()
        self._verification_start_time = start_time
        self._status = "starting"
        
        try:
            options = self._init_browser_options(user_agent)
            self._page = ChromiumPage(options)
            
            if self.config.screencast_video_path and self.config.screencast_video_path.strip():
                self._page.screencast.set_save_path(self.config.screencast_video_path)
                self._page.screencast.set_mode.video_mode()
                self._page.screencast.start()
            
            self._log('info', f"开始访问目标URL: {url}")
            self._page.get(url)
            
            # 验证码等待加载
            await asyncio.sleep(self.config.initial_wait_time)
            
            for attempt in range(self.config.max_attempts):
                self._log('debug', f'验证尝试 {attempt + 1}/{self.config.max_attempts}')
                if await self._handle_verification():
                    headers = self._extract_headers(url, user_agent)
                    self._status = "success"
                    
                    duration = (datetime.now() - start_time).total_seconds()
                    self._log('info', f'Turnstile验证完成，总用时: {duration:.2f}秒')
                    
                    # 保存headers到文件（可选）
                    if self.config.headers_output_path:
                        Path(self.config.headers_output_path).write_text(
                            'headers = ' + json.dumps(headers, ensure_ascii=False, indent=4),
                            encoding='utf-8'
                        )
                    
                    return headers
                
                await asyncio.sleep(self.config.wait_time)
            
            self._status = "failed"
            raise TurnstileError("达到最大尝试次数，验证失败")
            
        except asyncio.TimeoutError:
            self._status = "timeout"
            raise TurnstileTimeoutError("页面加载或验证超时")
            
        except Exception as e:
            self._status = "error"
            self._last_error = e
            raise TurnstileError(f"验证过程发生错误: {str(e)}")
            
        finally:
            if self._status != "success":
                duration = (datetime.now() - start_time).total_seconds()
                self._log('warning', f'Turnstile验证失败，总用时: {duration:.2f}秒')
            await self._cleanup()

    async def _cleanup(self):
        """清理资源"""
        if self._page:
            if self.config.screencast_video_path:
                self._page.screencast.stop()
            self._page.close()
            self._page = None

    @property
    def status(self) -> Dict[str, Any]:
        """获取当前状态信息"""
        return {
            "status": self._status,
            "start_time": self._verification_start_time,
            "duration": (datetime.now() - self._verification_start_time).total_seconds() if self._verification_start_time else None,
            "last_error": str(self._last_error) if self._last_error else None
        }
