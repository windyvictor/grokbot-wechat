# 微信接收可靠性：部署、回滚、上线后检查

本文件是给生产目录 `/workspace/grokbot-wechat` 的操作说明。这里的改动在公开 fork `windyvictor/grokbot-wechat`，不要推到上游 `psylch/grokbot-wechat`，也不要合并。

这次实现没有调用生产 webhook，没有调用 iLink，没有读取或复制 `credentials.json`、`.env`、`state.json`、`history.log` 或运行日志。下面说的“接收有进展”和“告警能送到微信”都还没有在生产上验证。

## 现场故障和代码里的原因

观察到的情况：接收进程不在，旧 pid 已无效，心跳最后停在 2026-11-12 01:21（北京时间），最后一次入站和 webhook 是 11 月 11 日 12:11。出站 `send.py` 不依赖接收进程，所以还能发。

对应到原来的代码：

- `run_forward.sh` 只在这个 shell 还活着的时候，把退出的 `forward.py` 过 5 秒再拉起。shell 自己没了，就没有接收器。
- `watchdog.sh` 不会自己跑。没有 cron、也没有定时 routine 去调用它时，机器或容器重启后没人拉进程。
- 生产机 PID1 是 tini，不是 systemd。`nohup` / `setsid` 只让进程脱离当前终端，容器停了它不会回来。
- 旧心跳在每轮循环开头就写一个时间戳，表示“进程还在转”，不表示“这一轮 getupdates 已经返回”。
- `ret != 0` 时先睡 10 秒、三次之后睡 60 秒，然后永远重试。`ret` 或 `errcode` 为 -14 是登录态失效，重试没有意义，只会打成风暴。

## 这次改了什么

- `supervise.sh` / `receive_supervisor.py`：同一时刻只有一个接收进程持有 `receiver.lock`。崩溃或轮询卡住按 2、4、8、16、32、60 秒退避重启。心跳是 JSON，并且带本进程 pid。上一轮留下的旧心跳不会用来杀掉刚拉起的子进程；子进程有 150 秒写出自己的心跳。它自己的 `in_poll` 超过 150 秒才算卡住。`auth_expired` 不会被当成卡住。接管时只向本目录里记录下来的 `forward.py` / `receive_supervisor.py` 发信号。
- `forward.py`：`-14` 写 `auth_expired`，通过现有 webhook 告警一次，然后停在原地不再调用 getupdates。`send.py` 不看这个状态。暂时网络错误指数退避，连续 5 次才告警一次，恢复后再告警一次。消息先写入 pending 或 quarantine 和 seen，然后才写游标。`message_id` 已在 pending 里则不再追加。
- 积压隔离：`WECHAT_QUARANTINE_BACKLOG=1` 时，做两件事，都不会 POST。一是把已经在 `pending.json` 里的消息整份挪进 `quarantine.json`（原因 `preexisting-pending`）。二是本轮新拉到的、时间早于进程启动或没有时间戳的消息也进入隔离区。全文保留。释放只改状态并拷进 pending，而且在写入 pending 完成前一直持有接收锁。
- `watchdog.sh`：监管进程还在，或心跳已是 `auth_expired` 时，不再拉起第二个轮询。
- `install_autostart.py`：只有 PID1 是 systemd 且 `systemctl is-system-running` 成功才安装 user unit，并回读 `is-enabled`；只有 `cron`/`crond` 真的在跑才写 crontab。否则退出码 2，不写任何单元。

入站 routine 模板增加了 `alert: true` 的分支：那不是用户消息，用 `send.py --app` 把 `text` 发到微信，不要执行里面的历史任务。线上 routine 不会因为这个仓库更新而自动改变，部署时要人工贴上这段。

## 在同类容器上核对过的自启动结果

在 PID1 为 tini、没有 cron 的环境里实际跑过（不是生产机）：

```text
python3 install_autostart.py --check
{"pid1": "tini", "systemd_running": false, "cron_running": false, "mechanism": "runtime-only", "installed": false}

python3 install_autostart.py --install
INSTALLED=no MECHANISM=runtime-only PID1=tini. ...
exit 2
```

没有创建 systemd unit。生产机也是 tini 时，预期相同。部署时必须在生产机上再跑一次 `--check`，不要把上面的输出当成生产机已经装好自启动。systemd 单元文本在代码里，但这次没有在 systemd 上执行过安装，不能说那条路径已验证。

## 部署

在生产 checkout 上做。先备份，再动文件。凭证和日志留在这台机器。

```bash
cd /workspace/grokbot-wechat
ts=$(date +%Y%m%d-%H%M%S)
bak="/workspace/grokbot-wechat-backup-$ts"
mkdir -p "$bak"
# 本地启动脚本、运行状态、凭证都留在这台机器的备份目录里，不要拷进公开仓库
cp -a run_forward.sh "$bak/" 2>/dev/null || true
cp -a start_forward.sh "$bak/" 2>/dev/null || true
cp -a .env credentials.json state.json pending.json outbox.json \
      forward_seen.json history.log forward.log forward.heartbeat forward.pid \
      "$bak/" 2>/dev/null || true
bash deploy/preflight.sh
git status --short
git diff -- run_forward.sh
```

`run_forward.sh` 在生产上有本地修改，`start_forward.sh` 未跟踪。不要 `git checkout --` 它们，也不要用仓库副本直接覆盖。

建议的拿代码方式：只把运行所需的这些文件从本 PR 拷进生产目录。不要覆盖生产上已修改的 `run_forward.sh`，不要动未跟踪的 `start_forward.sh`，不要替换 `.env`、`credentials.json` 或任何日志、游标、pending、outbox。

只要这些代码文件：

- `receive_supervisor.py`
- `forward.py`
- `ilink.py`
- `outbox.py`
- `supervise.sh`
- `watchdog.sh`
- `install_autostart.py`

`nohup` 仍然不是自启动。tini 上 `install_autostart.py --install` 退出码 2，不安装任何单元。容器入口要前台执行 `./supervise.sh`。

说明和测试可以另拷，但不是运行所必需：`deploy/RECEIVE_RECOVERY.md`、`deploy/preflight.sh`、`deploy/receive-reliability.patch`、`quarantine-rules.example.json`、`routines/`、`tests/test_receive_reliability.py`、`README.md`、`.gitignore`、`.env.example`（只读新增的变量说明，不要覆盖生产 `.env`）。

`.env.example` 只增加了说明。不要用它覆盖生产 `.env`。

生产上的 `run_forward.sh`：打开备份，把本地的路径和环境变量留住，让它最后执行 `./supervise.sh`，不要再 `while true; python3 forward.py; sleep 5`。那个 5 秒循环会在 `-14` 以外的退出上形成紧重启；`-14` 时新的 `forward.py` 自己不会退出，但旧循环仍是错误的入口。`start_forward.sh` 同样改成启动 `supervise.sh`，文件本身不要删。

第一次启动之前，在生产 `.env` 里加上（不要提交）：

```bash
WECHAT_QUARANTINE_BACKLOG=1
```

这是这次恢复的开关。游标还停在 11 月 11 日附近，不加这个变量，积压会在几秒内 POST 给 agent，11 月 16 日 13:53（北京时间）那条已经由双儿处理过的李建飞任务会被再执行一次。

可选的第二道规则，写到生产目录的 `quarantine-rules.json`（已被 gitignore，不要提交）。时间窗是 13:52–13:54：

```json
{
  "rules": [
    {
      "reason": "already-handled-2026-11-16T13:53+08",
      "text_contains": "<这条已处理消息里的独特片段>",
      "create_time_ms_min": 1794808320000,
      "create_time_ms_max": 1794808440000
    }
  ]
}
```

只靠文字片段不可靠，所以第一次启动仍然必须打开 `WECHAT_QUARANTINE_BACKLOG`。规则文件是额外保险，不是替代。

停掉本目录里旧的接收进程（不要动别的目录的 `forward.py`），然后启动：

```bash
# 先确认没有第二个轮询
pgrep -af forward.py || true
pgrep -af receive_supervisor.py || true
./supervise.sh
```

前台运行是这个容器里真实的保活方式：谁拉起容器，谁就要再执行 `supervise.sh`。`nohup setsid ./supervise.sh` 只在当前会话还在时有用，容器一停就没了。`python3 install_autostart.py --check` 若是 `runtime-only`，不要再跑一个会失败的安装然后以为已经开机自启。

把 `routines/inbound-webhook.md` 里 `alert == true` 那段贴进现有的微信入口 routine。不贴的话，告警 JSON 会到达 routine，但 agent 可能把它当成用户任务，而不是发到微信。

## 恢复后的审阅

接收进程跑起来之后，旧消息应出现在隔离区而不是 webhook：

```bash
python3 receive_supervisor.py quarantine-list
```

核对 2026-11-16 13:53 那条是 `held`，`text_preview` 对得上，然后不要释放它。其他需要补处理的消息逐条释放（接收进程要先停，否则内存里的 pending 会把文件盖掉）：

```bash
# 停 supervise.sh（对监管进程发 SIGTERM）
python3 receive_supervisor.py quarantine-release MESSAGE_ID
WECHAT_QUARANTINE_BACKLOG= ./supervise.sh
```

释放只把那一条放进 `pending.json`。隔离文件里的记录改成 `released`，payload 还在。未释放的继续 `held`。不要用“全部释放”。

游标在这一轮会前进。之后即使关掉 backlog 开关，已经进过 seen 的消息也不会再被拉进 webhook。所以开关必须在第一次启动前就打开。

## 回滚

1. 对监管进程发 SIGTERM，确认本目录没有 `forward.py` 还在轮询。
2. 用备份里的 `forward.py`、`run_forward.sh`、`watchdog.sh`、`start_forward.sh` 换回去。
3. 用原来的启动方式拉起接收器。
4. `quarantine.json` 可以留着，旧代码不读它。已经隔离、还没释放的消息不会自动补发。若回滚后仍要投递其中某条，先在新代码还在时 `quarantine-release`，确认它在 `pending.json` 里，再换回旧代码。
5. 不要把备份目录提交到公开仓库。

## 上线后由操作者做的检查

这些步骤这次没有做。

1. 接收有进展：没有新微信消息时，`forward.heartbeat` 里的 `updated_at` 仍大约每 40 秒更新，`status` 在 `in_poll` 和 `polling` 之间变化。这表示长轮询在返回，不是进程死了还留着旧心跳。
2. 单例：再开一个 `./supervise.sh`，它应马上退出，码 2，且只有一个 getupdates 在跑。
3. 隔离：`quarantine-list` 里能看到 11 月 16 日 13:53 那条，状态 `held`。随后的 webhook 批次里没有它。其他积压还在隔离文件里，文本还在。
4. 新消息：从微信发一条新的、与积压无关的消息。`forward.log` 里是 `route=pending`，routine 收到一批，且没有把隔离区里的旧任务再跑一遍。
5. 告警：单元测试覆盖了去重和“失败一次、恢复一次”。线上要能在微信里看到告警，必须先更新 routine。不要为了试告警去把生产 token 弄失效，也不要手动 POST 生产 webhook。真的出现 -14 或连续网络失败时，应只有一条 `alert: true` 的 webhook；routine 更新之后，微信里对应一条 `--app` 消息。恢复后同样一条。重复失败不应再刷。

成功的标准是第 1 步的心跳在前进，并且第 5 步的告警路径在 routine 更新后可用。在这两步于生产上做完之前，不要把这次改动说成端到端已经验证。
