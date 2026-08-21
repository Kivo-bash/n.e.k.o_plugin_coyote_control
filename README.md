# 郊狼控制插件 (Coyote Control Plugin)

让猫娘根据心情自主控制郊狼设备的 N.E.K.O 插件。插件本身就是 DG-Lab Socket V3 控制端：启动后直接开 WebSocket 服务，APP 扫码绑定后即可下发强度和波形。

## 功能特性

- 官方 V3 二维码：`https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#ws://IP:PORT/terminal_id`
- 心情系统：温柔、玩闹、调戏、惩罚
- 对话触发：监听聊天关键词，按概率输出
- 惩罚确认：惩罚模式需用户说「同意」才会执行
- 安全限制：强度上限、最小间隔、紧急停止词
- Hosted UI + 独立 Web 面板
- LLM 工具：`coyote_get_status` / `coyote_set_mood` / `coyote_toggle_control` / `coyote_emergency_stop`

## 安装依赖

```bash
cd plugin/plugins/coyote_control
pip3 install -r requirements.txt -t vendor/
```

## 配置说明

在 `plugin.toml` 的 `[coyote]` 部分：

```toml
[coyote]
server_host = "0.0.0.0"
server_port = 9998
web_host = "0.0.0.0"
web_port = 9006
safety_max_intensity_a = 50
safety_max_intensity_b = 50
idle_timeout_seconds = 300
trigger_probability = 0.3
emergency_keywords = ["停止", "够了", "疼", "stop", "pain"]
```

## 使用方式

1. 启动插件，打开「郊狼控制」面板
2. 用郊狼 APP 扫描官方格式二维码（手机和电脑同一 WiFi）
3. 连接成功后开启「自动控制」
4. 对话里出现心情关键词时，有概率触发对应波形
5. 不适时说「停止 / 够了 / 疼」或点紧急停止

## 心情模式

| 心情 | 强度范围 | 波形 | 说明 |
|------|---------|------|------|
| 温柔 (gentle) | 8-18 | 呼吸、气泡 | 轻柔舒适 |
| 玩闹 (playful) | 15-28 | 脉冲、潮汐、青蛙 | 活泼提醒 |
| 调戏 (teasing) | 22-38 | 攀爬、波浪、随机 | 刺激更强 |
| 惩罚 (punishment) | 30-45 | 脉冲、青蛙 | 需说「同意」 |

## 安全提示

1. 首次使用从低强度开始，把 `safety_max_intensity_*` 设低
2. 记住紧急停止词
3. 不适立刻说停止词或点面板按钮
4. 仅在安全、私密环境使用

## 故障排查

**插件加载失败**
- 把 `websockets` 和 `aiohttp` 装到 `vendor/`
- 查看插件日志

**APP 扫码连不上**
- 确认二维码是官方 `#DGLAB-SOCKET#` 格式
- 手机和电脑同一局域网，防火墙放行 9998
- 不要再用旧的 `ws://ip:port+xxx` 伪格式

**连上了但没有输出**
- 先开启自动控制
- 确认对话里出现了心情关键词
- 默认触发概率是 30%

## 开发信息

- SDK: `>=0.1.0,<0.3.0`
- 权限: `store`, `database`, `state:read`, `action:call`
- 版本: `0.2.0`
