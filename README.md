# grokbot-wechat · 把微信变成 Grok Bot 的聊天入口

[English summary below](#english-summary)

用腾讯官方的 **微信 ClawBot（iLink 协议）** 把你的微信接到 **Grok Bot**：
你在微信里给 ClawBot 发文字 / 语音 / 图片 / 文件 → 这台机器上的转发器长轮询收到 → 打包 POST 到 Grok Bot 的
**Webhook routine** → agent 处理后调用 `send.py` 回到微信。agent 也可以随时主动推送（提醒、日报），
推不出去的会排队，等你下次说话时自动补发。

纯 Python 标准库 + `cryptography`（媒体解密）+ `qrcode`（绑定二维码），没有框架，没有服务器，适合跑在 Grok Bot 的 box 或任何一台常开的 Linux / macOS 机器上。

> iLink 协议细节参考 [chihao.li 的拆解](https://chihao.li/blog/en/wechat-clawbot-ilink-protocol) 和腾讯的
> `@tencent-weixin/openclaw-weixin` npm 包。ClawBot 是官方功能（有服务条款），不是 hook / 逆向客户端。

---

## 架构

```
 微信 (ClawBot 插件)
   │  ▲
   │  │ ilink/bot/sendmessage            ┌──────────────── 这台机器 ─────────────────┐
   │  └───────────────────────────────────┤ send.py / outbox.py  ◄── agent 调用回复   │
   │                                      │      ▲ 推送失败 → outbox.json 排队          │
   ▼ ilink/bot/getupdates (长轮询 ~35s)   │      │ 用户一开口就补发；快到 24h 发 nudge   │
 ┌──────────────────────────────────┐     │                                            │
 │ forward.py                        │     │ supervise.sh（崩溃退避；-14 停止轮询）     │
 │  main    : 轮询 → 去重 → 下载解密媒体 → pending.json                              │
 │  flusher : 用户停顿 3s / 最多 60s → 整批 POST 到 webhook（带最近 40 行 history）   │
 │  outbox  : 每 15s 检查：补发队列 + keep-alive nudge                                │
 └──────────────┬───────────────────┘     │ supervise.sh（单例、退避、卡住检测、登录失效停轮询）│
                │ POST JSON + Bearer key  └────────────────────────────────────────────┘
                ▼
   Grok Bot routine「微信消息入口」(Webhook 触发) ──► agent 读消息/图片、整理、回复
                                                       └─► python3 send.py "…" --reply-to-ms <trigger_ms>
```

### 入站（微信 → Grok Bot）
1. `forward.py` 主线程循环调用 `ilink/bot/getupdates`，用 `state.json` 里的 `get_updates_buf` 当游标；
   超时 = 没消息，正常。
2. 只处理 `message_type == 1`（用户消息）；按 `message_id` 去重（`forward_seen.json`，保留最近 500 个）。
3. 每条消息记下 `context_token`（回复时要带）、`last_inbound_ms`（24h 窗口计时起点）。
4. 图片 / 语音 / 文件 / 视频：从微信 CDN 下载、AES-128-ECB 解密、按魔数猜扩展名，存到 `WECHAT_ATTACH_DIR`，
   把绝对路径放进 payload（详见「媒体」）。语音自带转写文字（`voice_item.text`）。
5. 消息先进 **pending.json**（落盘，进程重启不丢），由 flusher 线程在 **用户停顿 `BATCH_QUIET_S`（默认 3s）**
   或 **第一条已等 `BATCH_CAP_S`（默认 60s）** 时整批 POST。连发 5 条 = agent 只跑一轮。
6. POST 失败：4xx（除 408/429）不重试；其他指数退避 5 次；整批仍失败则保留，60s 后再试。
7. 成功后把这批写进 `history.log`（滚动的对话记录）。

### Webhook payload（agent 收到的东西）
```json
{
  "source": "wechat-clawbot", "batch": true, "count": 2,
  "trigger_ms": 1790000000000,
  "reply_rule": "Pass --reply-to-ms 1790000000000 to every send.py call. Exit code 4 (SUPERSEDED) means ...",
  "history": "[2026-10-01 21:00] User：…\n[2026-10-01 21:01] grok：…\n",
  "context_note": "(可选) Your last WeChat message to User was the keepalive nudge ...",
  "messages": [
    {"from_user_id": "<id>@im.wechat", "message_id": 123, "create_time_ms": 1789999990000,
     "texts": ["看看这张图"], "item_types": [1],
     "items": [{"type": 1, "type_name": "text", "text": "看看这张图"}]},
    {"message_id": 124, "create_time_ms": 1790000000000, "texts": [], "item_types": [2], "has_media": true,
     "items": [{"type": 2, "type_name": "image", "meta": {"...": "..."}, "file": "/abs/attachments/20261001-210000-image.jpg"}],
     "files": ["/abs/attachments/20261001-210000-image.jpg"]}
  ]
}
```

### 出站（Grok Bot → 微信）
- `send.py "文本"`：默认发给最后一个说话的人，带他最新的 `context_token`。
- `--reply-to-ms <trigger_ms>`：**可打断回复**。如果用户在 agent 思考期间又发了新消息
  （`last_inbound_ms` 比 trigger 新），直接 **exit 4 (SUPERSEDED)**，什么都不发——新一轮会连同旧消息一起回复。
  这样 batching 的停顿可以设得很短（3s），反应快又不会回答过时的问题。
- `--app`：主动推送（非回复）加前缀 `[grok bot] `，方便你在微信里区分。
- 推送被拒（24h 窗口已关）→ 写进 **outbox.json**，**exit 3 (QUEUED)**；转发器看到你下一条消息后 3 秒内按顺序补发
  （遇到第一条失败就停，下次再试）。
- 判断成功不能看 HTTP 状态码：iLink 拒绝时也返回 200，要看 JSON 里的 `ret` / `errcode`。
- 只有发送成功的文本才写进 `history.log`。

### 24 小时推送窗口 + keep-alive nudge + 免打扰
微信只允许 bot 在用户最后一次发言后的一段时间内主动发消息（实测 11h 能发、35h 失败，按 ~24h 处理）。
所以 `outbox.py` 在窗口快关时发一条很短的续期提醒（文案可在 `NUDGE_TEXT` 改）：

| 规则 | 值 |
|---|---|
| 正常 nudge 时段 | 最后一条消息后 **18h – 23h** |
| 免打扰 | **00:00–07:59 北京时间**（`QUIET_TZ` 固定时区，和机器时区无关）|
| 如果 18–23h 全落在免打扰里 | **提前**发：从 23h 往回找最后一个非免打扰时刻，在它 **30 分钟前**发；最早不早于 12h |
| 次数 | 每个窗口 **只试一次**（`state.json` 的 `nudged_for`）|

例：你早上 08:00 发了最后一条 → 18–23h 是次日 02:00–07:00，全在免打扰里 → 当晚 ~23:30 发 nudge。

你回个 "1" / "续" / "+1" 就续上了。为免 agent 每次都问"1 是什么意思"，forward.py 会在 payload 里加 `context_note`：
- 上一条 bot 消息就是 nudge → 告诉 agent 这是对 nudge 的回复，简短回"续上了"；
- 或者沉默 ≥12h 后只发了个 "1" → 同样当作续期。

### 接收监管
`supervise.sh` 跑 `receive_supervisor.py`，只允许一个接收进程持有 `receiver.lock`（避免两个 `getupdates` 抢同一个游标）。子进程 `forward.py` 崩溃或轮询心跳卡住时，按 2s、4s、8s… 上限 60s 重启。`forward.heartbeat` 是 JSON：一次长轮询开始时写成 `in_poll`，返回后才更新；超过 150 秒仍是 `in_poll` 视为卡住。登录态失效（`ret`/`errcode` = -14）停止轮询并只告警一次，进程留在原地不再请求，也不影响 `send.py`。

`nohup` / `setsid` 只是把进程从当前终端解开，**不是**重启持久化。容器 PID1 是 tini、并且没有在跑的 systemd 或 cron 时，容器或会话停了，接收器不会自己回来。`python3 install_autostart.py --check` 会说明这台机器能不能安装自启动；`--install` 只在 PID1 是 systemd 且 systemctl 正常时写 user unit 并回读 `is-enabled`，或在 cron 真的在跑时写入 crontab。否则退出码 2，什么都不安装。

`watchdog.sh` 是可选的外层检查（cron 或定时 routine 真的会调用它才有用）。心跳状态已是 `auth_expired` 时它不会再拉起轮询。部署和回滚见 `deploy/RECEIVE_RECOVERY.md`。

### 媒体
- 类型：`1` 文字、`2` 图片、`3` 语音（`voice_item.text` 是转写）、`4` 文件、`5` 视频。
- 下载地址：优先 `media.full_url`，否则 `https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=<URL 编码的 encrypt_query_param>`。
- 密钥两种格式：`aeskey`（hex）或 `media.aes_key`（base64，解出来可能是 16 字节原始 key，也可能是 32 字符 hex 字符串）。
- AES-128-ECB 解密后去 PKCS7 padding；扩展名优先用文件名，否则按魔数（jpg/png/gif/webp/pdf/zip/amr/silk/mp4）。
- 语音一般是 SILK，agent 通常直接用转写文字就够了。
- 下载失败不会丢消息：payload 里带 `media_error` 和 `raw_items`（原始 item，含 CDN 参数和 key，agent 可以自己重试），agent 至少知道"用户发了张图但没下下来"。
- 送给 webhook 的元数据里会剥掉 `media` / `thumb_media` / `aeskey`，长字符串截断。

---

## 安装

要求：Python 3.9+（用到 `zoneinfo`），能开 ClawBot 插件的微信（发布时为 iOS 微信 8.0.70+，灰度中），一个 Grok Bot 账号。

```bash
git clone https://github.com/psylch/grokbot-wechat.git && cd grokbot-wechat
pip install -r requirements.txt
cp .env.example .env            # 先空着 webhook，下一步再填
```

1. **绑定微信**：`python3 bind.py` → 用微信扫生成的 `qr.png`（或打开 `qr_url.txt` 里的链接）→ 手机上确认。
   成功后生成 `credentials.json`（权限 600，**绝不能提交**）。二维码过期会自动刷新；手机显示数字时，
   把数字写进同目录的 `verify_code.txt`。
2. **建入站 routine**：Grok Bot 里新建 routine，触发方式 Webhook，指令用 `routines/inbound-webhook.md`
   （把里面的路径换成你的）。把 URL 和 key 填进 `.env`。
3. **自检**：`python3 forward.py --selftest` 应该返回 `(200, ...)`，routine 会收到一条测试消息。
4. **启动**：在容器入口前台跑 `./supervise.sh`（或现有的长期会话里跑它）。`nohup setsid ./supervise.sh` 只能在这个会话还在时保活，容器重启不会把它拉回来。看 `tail -f forward.log`，心跳文件应变为 `polling`。在微信里给 ClawBot 发一句话试试。
5. **自启动**：`python3 install_autostart.py --check`。只有输出里的 mechanism 是 `systemd` 或 `cron` 时才运行 `--install`，并确认它打印 `INSTALLED=`。`runtime-only` 表示没有可安装的机制。可选的外层检查见 `routines/watchdog.md`。
6. **主动推送**：其他 routine（日报、提醒）里让 agent 调 `python3 send.py --app "…"`；exit 3 表示已排队，不用重试。

离线测试（不连网）：`python3 -m unittest discover -s tests -v`

### 文件一览
| 文件 | 作用 |
|---|---|
| `forward.py` | 常驻转发器：轮询、去重、媒体、批处理 POST、outbox 线程 |
| `outbox.py` | 推送失败排队 / 补发、keep-alive nudge、免打扰 |
| `send.py` | agent 用的发送命令（可打断、排队）|
| `ilink.py` | 最小 iLink 客户端（`post` / `send_text` / state 读写）|
| `media.py` | 媒体摘要、CDN 下载、AES 解密 |
| `history.py` | 滚动对话记录 `history.log`（2000 行时截到 1000）|
| `bind.py` | 扫码绑定，生成 `credentials.json` |
| `poll.py` | 调试用的一次性轮询（**转发器运行时别用**）|
| `config.py` | 读 `.env` / 环境变量 |
| `supervise.sh` / `receive_supervisor.py` | 单例接收监管：崩溃退避、卡住重启、登录失效停止 |
| `run_forward.sh` | 兼容入口，转到 `supervise.sh`。机器上的本地改动不要盲覆盖 |
| `watchdog.sh` / `install_autostart.py` | 可选外层检查；自启动只在 systemd 或 cron 真实可用时安装 |
| `routines/` | Grok Bot routine 指令模板 |

运行时生成（都在 `.gitignore` 里）：`credentials.json`、`state.json`（游标、context token、最近 20 条原始消息）、
`pending.json`、`outbox.json`、`forward_seen.json`、`history.log`、`forward.log` / `forward.out` / `forward.pid` / `forward.heartbeat`、`attachments/`。

## 不包含什么（需要你自己准备）
- **任何密钥**：`credentials.json`（iLink `bot_token`）、Webhook URL、Webhook Bearer key。
- 原作者的 `state.json` / `history.log` / `forward.log` / 下载的图片——里面都是真实聊天内容和微信 ID。
- 原作者在 Grok Bot 里实际使用的 routine 指令和 agent 人设（这里给的是等价模板）。

## 隐私提醒
- 每次 POST 会附带 `history.log` 最后 40 行，也就是你和 bot 的近期对话会发给 Grok Bot。
- `state.json` 存着 `context_token` 和最近 20 条原始消息，`forward.log` 里有微信用户 ID——都别外传。
- `credentials.json` 拿到就能以你的 ClawBot 身份收发消息。泄露了：在微信里解绑 ClawBot，再重新 `bind.py`。

## 踩过的坑
1. **24h 推送窗口**：用户太久没说话，bot 主动发会被拒，而且 **HTTP 仍是 200**，只能看 `ret` / `errcode`。→ outbox 排队 + nudge。
2. **nudge 半夜发 / 被免打扰吞掉**：第一版只在 18–23h 发、免打扰就跳过，结果窗口整段落在凌晨时直接错过。→ 改成提前到免打扰开始前 30 分钟发（最早 12h）。
3. **回 "1" 续期，agent 却反问"1 是什么意思"**：→ `context_note` 告诉它这是续期。
4. **连发几条，agent 回了好几轮、互相打架**：→ pending.json 批处理。停顿时间调过 60s → 25s → 20s → 3s，最大等待 300s → 60s；
   太长显得迟钝，太短会拆批。最后靠 `--reply-to-ms` 的「被新消息取代就闭嘴」才敢用 3s。
5. **agent 回答过时的问题**：用户在它思考时补充了内容。→ `trigger_ms` + `send.py --reply-to-ms` + exit 4。
6. **每次 webhook 都是新一轮，agent 不记得刚聊了啥**：→ `history.log` 拼接双方发言，每批带最近 40 行。
7. **`poll.py` 和 `forward.py` 同时跑会互相偷消息**（共用一个游标）；多个写者写 `state.json` 会覆盖。→ 别同时跑；读改写前重新读一遍，写用临时文件 + `os.replace`。`receiver.lock` 拒绝第二个接收进程。
8. **第一次收图片 CDN 返回 HTTP 400**（几分钟后的图片就正常了，具体修了哪一处没留记录）。现在 `media.py` 的做法：有 `full_url` 就优先用，否则把 `encrypt_query_param` 完整 URL 编码（`quote(safe="")`）拼 CDN 地址；AES key 的 hex / base64(raw) / base64(hex) 三种写法都认。
9. **长轮询偶发 `SSL: EOF occurred in violation of protocol`**：暂时网络失败，指数退避到 60 秒，成功后清零。`socket.timeout` 是正常的“没消息”，不计入失败。
10. **机器或容器重启后转发器没了**：`nohup` 不管这件事。PID1 是 tini 且没有 systemd/cron 时，只能让容器入口自己再执行 `supervise.sh`。监管进程活着时，子进程崩溃或轮询卡住会按上限退避重启。
11. **重复投递**：同一条消息可能来两次。→ `message_id` 去重。
12. **时区**：box 在 Asia/Taipei，免打扰按北京时间——都是 UTC+8，但代码里显式用 `Asia/Shanghai`，换机器也不会错。
13. **补发和 nudge 都不带 `context_token`**（`context_token=""`），旧 token 隔了很久可能失效；实测不带也能发。
14. **绑定**：二维码会过期（自动刷新，最多 `MAX_REFRESH` 次）；可能返回 `scaned_but_redirect`，要改用 `redirect_host`；可能要求输入手机上显示的数字（`need_verifycode`）。
15. **Webhook 重试**：4xx（除 408/429）是配置错（URL / key 不对），重试没用，直接放弃并保留 pending，修好配置后重启即可补发。
16. **getupdates 返回 -14**：登录态失效。继续轮询没有用，还会打成重试风暴。接收器停止拉消息、通过现有 webhook 告警一次；`send.py` 不受影响。重新 `bind.py` 之后再启动。
17. **长时间停收后的积压**：游标还停在停机前，`pending.json` 里也可能还有没发出去的消息。第一次恢复用 `WECHAT_QUARANTINE_BACKLOG=1`：已有 pending 和新拉到的旧消息都进 `quarantine.json`，不自动 POST。先审再释放应该投递的消息。规则文件不要提交。

---

## English summary

**grokbot-wechat** bridges WeChat (via Tencent's official **ClawBot / iLink** bot protocol) to a **Grok Bot**
webhook routine, so WeChat becomes a chat front-end for your agent.

- **Inbound**: `forward.py` long-polls `ilink/bot/getupdates`, dedupes, downloads + AES-128-ECB-decrypts media from the
  WeChat CDN, buffers messages in a disk-backed `pending.json`, and POSTs them as **one batch** once you pause (3s) or after 60s,
  together with the last 40 lines of a stitched `history.log` (each webhook run is a fresh agent run).
- **Outbound**: the agent replies with `send.py "text" --reply-to-ms <trigger_ms>`. If you've sent something newer
  meanwhile, it exits **4 (SUPERSEDED)** and sends nothing — replies are interruptible. If WeChat refuses (the ~24h
  push window since your last message has closed; note iLink still returns HTTP 200, check `ret`/`errcode`), the text
  is queued in `outbox.json` (exit **3**) and auto-sent right after you next write.
- **Keep-alive nudge**: one short "reply 1 to renew" message 18–23h after your last message, never during quiet
  hours (00:00–07:59 Beijing); if that slot is all quiet hours, it's sent early (≥12h) 30 min before quiet hours start.
  A bare "1" gets a `context_note` so the agent just acks it.
- **Supervisor**: `supervise.sh` keeps a single `forward.py`. Crashes and a stalled long-poll restart with capped backoff. `ret`/`errcode` -14 stops polling and alerts once through the existing webhook; `send.py` is unchanged. `nohup` does not survive a container restart. `install_autostart.py` installs a unit only where systemd or cron is actually running.
- **Secrets are not included**: run `bind.py` to get your own `credentials.json`; put your webhook URL/key in `.env`
  (see `.env.example`). Runtime state, logs and media are git-ignored.

Setup: `pip install -r requirements.txt` → `python3 bind.py` (scan QR) → create a webhook routine
(`routines/inbound-webhook.md`) → fill `.env` → `python3 forward.py --selftest` → run `./supervise.sh` in the foreground
→ `python3 install_autostart.py --check` (install only if it reports systemd or cron). Offline tests: `python3 -m unittest discover -s tests -v`.

Protocol credits: [chihao.li — WeChat ClawBot iLink protocol breakdown](https://chihao.li/blog/en/wechat-clawbot-ilink-protocol),
`@tencent-weixin/openclaw-weixin`. Use within the WeChat ClawBot Terms of Service. MIT licensed.
