"""
郊狼控制插件 - 让猫娘根据心情自主控制郊狼设备
"""
import sys
from pathlib import Path

vendor_path = Path(__file__).parent / "vendor"
if vendor_path.exists():
    sys.path.insert(0, str(vendor_path))

import asyncio
import random
import threading
import time
import uuid
from typing import Dict, Any, Optional

from plugin.sdk.plugin import (
    NekoPluginBase,
    neko_plugin,
    plugin_entry,
    lifecycle,
    llm_tool,
    ui,
    tr,
    Ok,
    Err,
    SdkError,
)

from .dglab_server_v4 import DglabServerV4
from .mood_engine import MoodEngine, CONTROL_MODES
from .safety_limiter import SafetyLimiter, SafetyError
from .waveforms import get_waveform_frames
from .web_server import CoyoteWebServer


@neko_plugin
class CoyoteControlPlugin(NekoPluginBase):
    """郊狼控制插件"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        self.dglab_server: Optional[DglabServerV4] = None
        self.mood_engine: Optional[MoodEngine] = None
        self.safety_limiter: Optional[SafetyLimiter] = None
        self.web_server: Optional[CoyoteWebServer] = None

        self._server_loop: Optional[asyncio.AbstractEventLoop] = None
        self._server_thread: Optional[threading.Thread] = None
        self._web_loop: Optional[asyncio.AbstractEventLoop] = None
        self._web_thread: Optional[threading.Thread] = None
        self._state_lock = threading.Lock()
        self._llm_tool_guard_task: Optional[asyncio.Task] = None

        self.server_host = "0.0.0.0"
        self.server_port = 9998
        self.web_host = "0.0.0.0"
        self.web_port = 9006
        self.web_enabled = True

        self.config_data = {}
        self.logger.info("CoyoteControlPlugin initialized")

    def _apply_config(self, cfg: Dict[str, Any]):
        self.config_data = cfg or {}
        coyote_config = self.config_data.get("coyote", {})
        self.server_host = coyote_config.get("server_host", "0.0.0.0")
        self.server_port = coyote_config.get("server_port", 9998)
        self.web_host = coyote_config.get("web_host", "0.0.0.0")
        self.web_port = coyote_config.get("web_port", 9006)
        self.web_enabled = coyote_config.get("web_enabled", True)
        self.safety_limiter = SafetyLimiter(self.config_data, self.logger)

    def _load_or_create_controller_id(self) -> str:
        """持久化控制方 controller_id：重启不变化，APP 无需重扫配对"""
        state_file = Path(__file__).parent / "data" / "controller_id.txt"
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            if state_file.exists():
                cid = state_file.read_text(encoding="utf-8").strip()
                if cid:
                    return cid
            cid = str(uuid.uuid4())
            state_file.write_text(cid, encoding="utf-8")
            self.logger.info("Created persistent controller_id: %s", cid)
            return cid
        except Exception as error:
            self.logger.warning("Failed to persist controller_id: %s", error)
            return str(uuid.uuid4())

    @lifecycle(id="startup")
    async def startup(self, **_):
        """启动插件"""
        try:
            cfg = await self.config.dump(timeout=5.0)
            self._apply_config(cfg)
            self.mood_engine = MoodEngine(self.logger)

            self._start_websocket_server()
            
            # 启动独立 Web 服务器（如果启用）
            if self.web_enabled:
                self._start_web_server()

            if not self.store.enabled:
                self.store.enabled = True

            # 守护 LLM 工具注册：main_server 的工具注册表是内存态，
            # 重启后注册会丢失且 SDK 不自动补注册（见 tool-calling 文档），
            # 这里定期探测并自动重新注册。
            self._llm_tool_guard_task = asyncio.create_task(self._llm_tool_guard())

            self.logger.info("CoyoteControlPlugin startup complete")
            return Ok({
                "status": "ready",
                "server_url": f"ws://{self.server_host}:{self.server_port}",
                "web_url": f"http://{self.web_host}:{self.web_port}" if self.web_enabled else None,
            })
        except Exception as error:
            self.logger.error("Startup failed: %s", error)
            return Err(SdkError(f"Startup failed: {error}"))

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        try:
            cfg = await self.config.dump(timeout=5.0)
            self._apply_config(cfg)
            return Ok({"status": "config_updated"})
        except Exception as error:
            self.logger.error("Config change failed: %s", error)
            return Err(SdkError(f"Config change failed: {error}"))

    def _main_server_port(self) -> int:
        """读取 main_server 实际端口（端口被占用时 NEKO 会顺延）"""
        try:
            import json as _json
            cfg = (
                Path.home()
                / "Library" / "Application Support" / "N.E.K.O" / "port_config.json"
            )
            data = _json.loads(cfg.read_text(encoding="utf-8"))
            return int(data.get("MAIN_SERVER_PORT", 48911))
        except Exception:
            return 48911

    async def _llm_tool_guard(self):
        """定期检查 main_server 工具注册表，丢失时自动重新注册。

        main_server 的 ToolRegistry 是内存态：main_server 重启、或插件
        启动早于 main_server 就绪时，@llm_tool 的注册会静默丢失，
        猫娘对话就永远看不到工具。SDK 不做自动恢复，这里自己兜底。
        """
        import aiohttp

        await asyncio.sleep(10)
        while True:
            try:
                port = self._main_server_port()
                url = f"http://127.0.0.1:{port}/api/tools"
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=3)
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        data = await resp.json()

                registered_names = set()
                for tools in (data.get("tools_by_role") or {}).values():
                    for tool in tools:
                        registered_names.add(tool.get("name"))

                missing = [
                    meta
                    for name, meta in getattr(self, "_llm_tools", {}).items()
                    if name not in registered_names
                ]
                for meta in missing:
                    self.logger.warning(
                        "LLM tool '%s' missing from main_server registry, re-registering",
                        meta.name,
                    )
                    self._notify_llm_tool_registered(meta)
            except asyncio.CancelledError:
                break
            except Exception as error:
                # main_server 未就绪/重启中，静默等待下一轮
                self.logger.debug("LLM tool guard probe failed: %s", error)
            await asyncio.sleep(30)

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        """关闭插件"""
        try:
            if self._llm_tool_guard_task:
                self._llm_tool_guard_task.cancel()
                self._llm_tool_guard_task = None
            if self.web_server and self._web_loop:
                try:
                    if hasattr(self.web_server, 'stop') and callable(self.web_server.stop):
                        stop_coro = self.web_server.stop()
                        if stop_coro is not None:
                            # result() 拿到的是 coroutine 的返回值(可能为 None)，
                            # 不能再 await；await None 会抛 TypeError
                            asyncio.run_coroutine_threadsafe(
                                stop_coro, self._web_loop
                            ).result(timeout=3.0)
                except Exception as error:
                    self.logger.error("Stop web server failed: %s", error)
                finally:
                    self._stop_loop(self._web_loop)

            if self.dglab_server and self._server_loop:
                try:
                    await self._server_call(self.dglab_server.reset_all())
                    await self._server_call(self.dglab_server.stop())
                except Exception as error:
                    self.logger.error("Stop websocket server failed: %s", error)
                finally:
                    self._stop_loop(self._server_loop)

            self.logger.info("CoyoteControlPlugin shutdown complete")
            return Ok({"status": "stopped"})
        except Exception as error:
            self.logger.error("Shutdown error: %s", error)
            return Err(SdkError(f"Shutdown error: {error}"))

    def _start_websocket_server(self):
        """在独立线程中启动 WebSocket 服务器"""
        ready = threading.Event()

        def run_server():
            self._server_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._server_loop)

            self.dglab_server = DglabServerV4(
                host=self.server_host,
                port=self.server_port,
                logger=self.logger,
                controller_id=self._load_or_create_controller_id(),
            )
            self.dglab_server.on_client_attached = self._on_app_bound
            self.dglab_server.on_client_detached = self._on_app_unbound

            try:
                self._server_loop.run_until_complete(self.dglab_server.start())
                ready.set()
                self.logger.info(
                    "WebSocket server thread started on ws://%s:%s",
                    self.server_host,
                    self.server_port,
                )
                self._server_loop.run_forever()
            except Exception as error:
                self.logger.error("WebSocket server error: %s", error)
                ready.set()

        self._server_thread = threading.Thread(
            target=run_server,
            daemon=True,
            name="coyote-websocket-server",
        )
        self._server_thread.start()
        if not ready.wait(timeout=5.0):
            self.logger.warning("WebSocket server startup timed out")

    def _start_web_server(self):
        """在独立线程中启动 9006 Web 服务器（备用面板）"""
        ready = threading.Event()

        def run_web():
            self._web_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._web_loop)

            self.web_server = CoyoteWebServer(
                host=self.web_host,
                port=self.web_port,
                dglab_server=self.dglab_server,
                logger=self.logger,
            )

            try:
                self._web_loop.run_until_complete(self.web_server.start())
                ready.set()
                self.logger.info(
                    "Web server thread started on http://%s:%s",
                    self.web_host,
                    self.web_port,
                )
                self._web_loop.run_forever()
            except Exception as error:
                self.logger.error("Web server error: %s", error)
                ready.set()

        self._web_thread = threading.Thread(
            target=run_web,
            daemon=True,
            name="coyote-web-server",
        )
        self._web_thread.start()
        if not ready.wait(timeout=5.0):
            self.logger.warning("Web server startup timed out")

    def _stop_loop(self, loop: Optional[asyncio.AbstractEventLoop]):
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

    async def _server_call(self, coro, timeout: float = 5.0):
        if not self._server_loop:
            raise RuntimeError("Server event loop is not ready")
        future = asyncio.run_coroutine_threadsafe(coro, self._server_loop)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)

    def _on_app_bound(self, app_id: str):
        self.logger.info("APP bound: %s", app_id)

    def _on_app_unbound(self, app_id: str):
        self.logger.info("APP unbound: %s", app_id)

    def _get_local_ip(self) -> str:
        """获取本机 IP 地址"""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("8.8.8.8", 80))
                return sock.getsockname()[0]
            finally:
                sock.close()
        except Exception as error:
            self.logger.warning("Failed to get local IP: %s", error)
            return "127.0.0.1"

    def _is_app_connected(self) -> bool:
        return bool(self.dglab_server and self.dglab_server.has_bound_app())

    def _official_qr(self) -> str:
        local_ip = self._get_local_ip()
        if self.dglab_server and self.dglab_server.is_running():
            return self.dglab_server.get_qr_content(local_ip)
        return ""

    # ==================== Hosted UI Context ====================
    @ui.context(id="dashboard")
    async def dashboard(self):
        """提供 Web 面板状态数据"""
        server_running = bool(self.dglab_server and self.dglab_server.is_running())
        connected_clients = self.dglab_server.get_clients_detail() if server_running else []
        local_ip = self._get_local_ip()
        server_url = f"ws://{local_ip}:{self.server_port}"
        
        return {
            "server_running": server_running,
            "connected": self._is_app_connected(),
            "connected_clients": connected_clients,
            "qr_code_data": self._official_qr(),
            "ws_url": server_url,
            "local_ip": local_ip,
            "server_port": self.server_port,
            "status_message": f"服务器运行中: {server_url}" if server_running else "服务器未启动",
        }

    # ==================== UI Action - Test Connection ====================
    @ui.action(
        label=tr("actions.test.label", default="测试连接"),
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="test_connection",
        name=tr("entries.test.name", default="测试连接"),
        description=tr("entries.test.description", default="发送测试波形验证设备连接"),
    )
    async def test_connection(self, **_):
        """测试连接 - 发送 10 功率脉冲 5 秒"""
        if not self._is_app_connected():
            return Err(SdkError("设备未连接"))

        try:
            # 生成 10 功率的 PULSE 波形，持续 5 秒
            frames = get_waveform_frames("PULSE", intensity=10)
            duration_ms = 5000

            # 先设置临时强度(SetTempIntensity)，再下发波形帧，APP 强度数值才会变化
            await self._server_call(
                self.dglab_server.apply_pulse(
                    channel="both",
                    intensity=10,
                    waveform_hex_list=frames,
                )
            )

            self.logger.info("Test pulse sent: 10 power, 5s")
            return Ok({"message": "测试波形已发送（10功率，5秒）"})
        except Exception as error:
            self.logger.error("Test connection failed: %s", error)
            return Err(SdkError(f"测试失败: {error}"))

    # ==================== LLM Tool - Control Device ====================
    @llm_tool(
        name="control_coyote",
        description=(
            "控制郊狼设备：按模式设置临时强度并下发对应波形。"
            "可用模式: reward_gentle(温柔奖励), reward_playful(活泼奖励), "
            "tease_light(轻度调戏), punish_mild(轻度惩罚), punish_strong(强力惩罚)。"
            "custom_intensity 会真实设置设备强度，不填则用模式默认强度；"
            "custom_duration_ms 为临时强度持续时间，到时自动归零。"
            "只在设备已连接且情境合适时调用。"
            "【重要】只要你在回复中描写、宣称或暗示对郊狼设备执行了任何操作"
            "（电击/奖励/惩罚/调戏等），就必须实际调用本工具或 set_coyote_intensity，"
            "严禁只在台词或括号动作里扮演而不调用工具——那样设备不会有任何反应。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(CONTROL_MODES.keys()),
                    "description": "控制模式",
                },
                "reason": {
                    "type": "string",
                    "description": "使用此模式的原因（用于日志记录）",
                },
                "custom_intensity": {
                    "type": "integer",
                    "description": "可选：自定义强度 (0-200，郊狼设备上限)，不填则使用模式默认范围",
                },
                "custom_duration_ms": {
                    "type": "integer",
                    "description": "可选：自定义持续时间（毫秒），不填则使用模式默认范围",
                },
            },
            "required": ["mode", "reason"],
        },
    )
    async def control_coyote(
        self,
        *,
        mode: str,
        reason: str,
        custom_intensity: Optional[int] = None,
        custom_duration_ms: Optional[int] = None,
    ):
        """LLM Tool: 控制郊狼设备"""
        if not self._is_app_connected():
            return {"success": False, "error": "设备未连接"}

        try:
            # 生成控制参数
            params = self.mood_engine.generate_params(
                mode=mode,
                custom_intensity=custom_intensity,
                custom_duration_ms=custom_duration_ms,
            )

            # 安全检查
            try:
                self.safety_limiter.validate_pulse(
                    intensity=params["intensity"],
                    duration_ms=params["duration_ms"],
                )
            except SafetyError as error:
                self.logger.warning("Safety check failed: %s", error)
                return {"success": False, "error": f"安全检查失败: {error}"}

            # 生成波形
            frames = get_waveform_frames(
                params["waveform"],
                intensity=params["intensity"],
            )

            # 发送到设备：先设置临时强度，再下发波形帧（custom_intensity 真实生效）
            await self._server_call(
                self.dglab_server.apply_pulse(
                    channel=params["channel"],
                    intensity=params["intensity"],
                    waveform_hex_list=frames,
                    duration_ms=params["duration_ms"],
                )
            )

            self.logger.info(
                "Control executed: mode=%s, intensity=%d, duration=%dms, reason=%s",
                mode,
                params["intensity"],
                params["duration_ms"],
                reason,
            )

            return {
                "success": True,
                "mode": mode,
                "intensity": params["intensity"],
                "duration_ms": params["duration_ms"],
                "waveform": params["waveform"],
                "description": params["description"],
            }

        except Exception as error:
            self.logger.error("Control failed: %s", error)
            return {"success": False, "error": str(error)}

    # ==================== LLM Tool - Set Intensity ====================
    @llm_tool(
        name="set_coyote_intensity",
        description=(
            "直接设置郊狼设备指定通道的强度。强度值 0-200（设备上限 200）。"
            "钳制上限优先使用 APP 端上报的最大强度(intensityMax)；"
            "仅当 APP 未上报最大强度时，才使用插件的安全上限作为兜底。"
            "强度 0 表示归零。不传 duration_ms 则强度持续保持(不自动归零)，"
            "传了则到时自动归零。"
            "返回实际设置的强度、APP 端最大强度、以及兜底安全上限。"
            "只在设备已连接时调用。"
            "【重要】只要你在回复中描写、宣称或暗示调整了设备强度"
            "（拉满/加到XX/归零等），就必须实际调用本工具执行，"
            "严禁只在台词或括号动作里扮演而不调用工具——那样设备不会有任何反应。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "enum": ["A", "B", "both"],
                    "description": "通道：A / B / both",
                },
                "intensity": {
                    "type": "integer",
                    "description": "目标强度 (0-200，设备上限 200)，0 表示归零",
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "可选：临时强度持续时间（毫秒）；不传则持续保持不自动归零，传了则到时自动归零",
                },
                "reason": {
                    "type": "string",
                    "description": "设置原因（用于日志记录）",
                },
            },
            "required": ["channel", "intensity", "reason"],
        },
    )
    async def set_coyote_intensity(
        self,
        *,
        channel: str,
        intensity: int,
        reason: str,
        duration_ms: Optional[int] = None,
    ):
        """LLM Tool: 直接设置设备强度"""
        if not self._is_app_connected():
            return {"success": False, "error": "设备未连接"}

        try:
            clients = self.dglab_server.get_connected_clients()
            if not clients:
                return {"success": False, "error": "设备未连接"}
            client_id = clients[0]

            # 安全上限
            safety_max = (
                self.safety_limiter.max_intensity_a
                if channel in ("A", "both")
                else self.safety_limiter.max_intensity_b
            )
            if channel == "both":
                safety_max = min(
                    self.safety_limiter.max_intensity_a,
                    self.safety_limiter.max_intensity_b,
                )

            # APP 端上报的通道强度上限（slotState.channelX.intensityMax）
            client_info = self.dglab_server.clients_info.get(client_id, {})
            app_max_a = client_info.get("intensity_max_a")
            app_max_b = client_info.get("intensity_max_b")
            app_max = None
            if channel == "both":
                if app_max_a is not None and app_max_b is not None:
                    app_max = min(app_max_a, app_max_b)
                else:
                    app_max = app_max_a if app_max_a is not None else app_max_b
            else:
                app_max = app_max_a if channel.upper() == "A" else app_max_b

            # 钳制上限：优先 APP 端上报的强度上限，无上报时才用安全上限
            max_allowed = app_max if app_max is not None else safety_max
            clamped = max(0, min(int(intensity), max_allowed))
            if clamped != intensity:
                self.logger.warning(
                    "Intensity %d exceeds limit %d, clamped to %d",
                    intensity,
                    max_allowed,
                    clamped,
                )

            # duration=0 表示不自动结束（持续保持）；传了则在到时后自动归零
            duration = duration_ms if duration_ms is not None else 0
            ok = True
            channels = ["A", "B"] if channel == "both" else [channel.upper()]
            for ch in channels:
                ok = await self._server_call(
                    self.dglab_server.set_strength(
                        client_id, ch, clamped, duration_ms=duration
                    )
                ) and ok
                await asyncio.sleep(0.05)

            self.logger.info(
                "Set intensity: channel=%s, intensity=%d, duration=%dms, reason=%s, ok=%s",
                channel,
                clamped,
                duration,
                reason,
                ok,
            )

            return {
                "success": ok,
                "channel": channel,
                "intensity": clamped,
                "duration_ms": duration,
                "max_intensity": max_allowed,
                "app_max_intensity": app_max,
                "safety_max_intensity": safety_max,
                "clamped": clamped != intensity,
            }

        except Exception as error:
            self.logger.error("Set intensity failed: %s", error)
            return {"success": False, "error": str(error)}
