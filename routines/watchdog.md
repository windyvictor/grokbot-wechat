# Routine 模板 ②：转发器看门狗（每 15 分钟）

> 在 Grok Bot 里建一个 **routine**，触发方式选 **定时（every 15 minutes）**，在跑 forward.py 的那台机器（box）上执行。
> 也可以不用 routine，改用 cron：`*/15 * * * * /path/to/grokbot-wechat-bridge/watchdog.sh >> /path/to/data/watchdog.log 2>&1`

---

**指令（贴进 routine）：**

运行 `bash /path/to/grokbot-wechat/watchdog.sh`，它只输出一行：

- `OK …`：监管进程或本目录的接收进程已在。**什么都不用做，不要发任何消息，安静结束。**
- `RESTARTED (supervisor absent) pid=…`：监管进程不在，已拉起。不用再手动启动第二个。
- `AUTH_EXPIRED …`：登录态失效，脚本**不会**重启轮询。不要循环启动，也不要再发一条微信。接收器已经用 webhook 告警过一次。安静结束。重新绑定由人在机器上做完 `bind.py` 后再启动 `supervise.sh`。
- `FAILED …`：拉不起来。看 `supervisor.out` 和 `forward.log` 末尾。常见原因：缺依赖、`credentials.json` 丢失、另一个接收进程占着锁。修不了就把原因简短告诉用户。

`nohup` 不能代替这条 routine 或 cron。容器 PID1 是 tini 且没有 cron/systemd 时，这条指令本身也不会在重启后自动出现。

注意：
- 不要同时手动再启一个 `run_forward.sh`，两个轮询器共用一个 getupdates 游标会互相"偷"消息。
- 不要在转发器运行时跑 `poll.py`（同理）。
