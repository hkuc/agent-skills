# 参数、续传与故障处理

## 命令行参数

`python3 "<SKILL_DIR>/scripts/download.py" --help` 为完整参数说明。`<SKILL_DIR>` 是当前技能目录的绝对路径；Windows 可使用 `py -3` 替代 `python3`。

| 参数 | 行为 |
| --- | --- |
| URL 或 `--url-file` | 二选一；文件为只包含一个 URL 的 UTF-8 文本。仅 HTTP/HTTPS，不接受 URL 用户名密码或 fragment。 |
| `-o` / `--output` | 必填，指定最终文件路径。为便于复现，调用时使用绝对路径。 |
| `--threads` | 默认 8，范围 1～32。 |
| `--chunk-size` | 默认 64MiB；支持整数（字节）、B、KiB、MiB、GiB，最大 1GiB。续传时保持一致。 |
| `--retries` | 默认 3 次额外重试，0～10。等待 1、2、4……秒，上限 60 秒；不是无限重试。 |
| `--timeout` | 默认 30 秒，范围 (0, 3600]；约束单次网络阻塞，不是任务总时长。 |
| `--sha256` | 可信预期值，64 位十六进制，不区分大小写；与下载所得哈希比较。 |
| `--headers-file` | UTF-8 JSON 对象，名称和值均为字符串。支持 Authorization、Cookie 及自定义头。 |
| `--overwrite` | 验证成功后原子替换目标，失败不改原文件；需用户明确同意。 |
| `--restart` | 把旧 `active` 改名为 `retained-随机ID`，重新下载；不会清理旧任务。 |
| `--json` | stdout 仅最终一个 JSON 对象，进度到 stderr，两者使用 UTF-8。参数解析错误仍使用标准 argparse stderr。 |

默认分片 64MiB，即 67,108,864 字节；8 线程不表示固定切成 8 片。比如 1GiB 文件分成 16 片，由最多 8 个工作线程调度。

## 带鉴权的调用

在用户指定的安全位置准备请求头文件，例如：

```json
{
  "Authorization": "Bearer <token>",
  "Cookie": "<cookie>",
  "X-Api-Key": "<api-key>"
}
```

只填服务实际需要的字段。文件包含凭据，不要放入仓库、压缩包或共享输出；在 Unix 上建议设为 `chmod 600`，Windows 使用对应文件 ACL。这些输入文件由调用者管理，下载器不会自动删除。

```bash
python3 "<SKILL_DIR>/scripts/download.py" \
  --url-file "/absolute/private/download-url.txt" \
  --headers-file "/absolute/private/download-headers.json" \
  --output "/absolute/path/model.bin" --json
```

- URL 签名和请求头不写日志或任务记录。记录中的 SHA-256 摘要用于身份比较，并非密钥加密存储；仍应保护整个工作目录。
- 签名 URL 或令牌更新会改变任务身份，默认不复用旧分片。需要时经用户同意使用 `--restart`，而不是忽略身份检查。
- 源由协议、主机和有效端口共同决定；跨源跳转移除全部用户自定义头，避免漏掉自定义 API-Key。跨源下载若因此返回 401/403，需要用户提供允许直接访问目标的凭据或签名链接；不自动放开转发。
- 用户不能覆盖 Range、条件请求、Host、Content-Length、Accept-Encoding 等内部保留头；每次请求都使用 `identity` 编码。
- 不禁用 TLS 证书验证。代理与信任库使用 Python/系统环境设置，例如调用者已配置的代理环境变量；本 skill 不修改全局网络配置。

## 临时目录与续传

若输出为 `/downloads/model.bin`，布局为：

```text
/downloads/model.bin                  # 仅校验通过才发布
/downloads/.model.bin.download/
  .lock                              # 无凭据的进程锁；成功后也保留
  active/
    manifest.json                    # 参数与远端身份摘要、完成分片大小/哈希
    part-00000000.bin                 # 完整分片
    part-00000001.bin.partial         # 正在下载或中断的分片
    merged.tmp                       # 合并中的候选文件
  retained-<id>/                     # 显式 --restart 保留的旧任务，可有多个
```

重新执行同一命令恢复。**旧版本按 16MiB 建立的任务，更新默认值后需显式传入 `--chunk-size 16MiB` 才能继续复用；不会自动迁移或重新切分旧分片。**线程数、重试次数、超时可以改变；URL、请求头、最终重定向目标、预期哈希、分片大小和资源版本需一致。某片缺失、截断或本地哈希变化会重新下载该片，不会复用半片。单线程文件不复用。

强 ETag 用于资源版本条件请求；有可信预期 SHA-256 时允许没有强 ETag 的多线程下载，但最终仍必须匹配该 SHA-256。弱 ETag 不用于可靠版本判断。若远端版本变化，默认停止，不自动删除旧数据。

`.lock` 必须保持同一个文件，避免两个进程分别锁住两个不同文件。操作系统在退出或崩溃时释放锁，不需手动删锁。**不要在任务运行时删除或移动工作目录。**如果暂时清理失败且正式文件已发布，先确认结果中的验证状态和文件哈希，再在没有任务运行时手动清理 `active`。

多线程合并需要分片和候选文件同时存在，建议在目标盘预留约两倍文件大小的可用空间；覆盖已有文件和保留的旧任务还需额外空间。临时数据和目标位于同一磁盘，便于原子发布。下载目录应仅由可信用户操作；不设计为抵御拥有同目录写权限的恶意进程的完整沙箱。

## 成功结果与校验等级

结果字段：

- `status`: `complete` 或 `complete-with-cleanup-warning`。
- `output`, `bytes`, `sha256`: 正式路径、实际字节数与最终计算所得哈希。
- `verification`: `sha256` 表示与可信预期值一致；`size-and-structure` 仅表示大小、分片结构、分片持久化与合并一致性校验通过。
- `mode`: `parallel` / `single`。`threads` 是本次工作线程数上限，不是实时活跃线程统计。
- `resumed_parts`: 本次恢复时通过本地重新校验并复用的分片数量。
- `temporary_parts_cleaned`: 本次临时分片是否已清理；旧 `retained-*` 不在清理范围。
- `temporary_directory`, `lock_file`, `note`: 临时路径、锁文件路径和校验说明。

自行计算的分片或最终 SHA-256 只能证明后续读取、合并是否与已收到的字节一致；**没有独立的可信预期值，就不能保证下载内容与发布者原件完全相同，也不能保证内容安全或压缩包可以解压。**本工具不会执行下载的文件，也不进行格式专用校验。

## 失败和退出码

| 退出码 | 含义与处理 |
| --- | --- |
| 0 | 文件已验证并发布；仍需检查是否有清理警告。 |
| 2 | 输入参数、URL 或请求头格式不正确。修正后重试。 |
| 3 | 网络/HTTP 错误；可重试故障预算已用尽，或非重试状态码。检查网络、鉴权、服务。 |
| 4 | 大小/范围/哈希错误，或无法确认完整性。不发布、不清理；检查可信哈希与资源地址。 |
| 5 | 同名文件、并行任务、资源版本或续传身份冲突。不要擅自加覆盖/重启参数。 |
| 6 | 本地 I/O、路径、安全发布或未预期错误。检查磁盘空间、权限和文件系统。 |
| 130 | Ctrl+C / SIGTERM 中断，保留续传现场。 |

异常不会回显原始 URL、请求头或服务器正文。SIGINT/SIGTERM 会等待正在进行的网络阻塞结束后退出（受 `--timeout` 约束）；提交已验证文件及清理的短阶段会暂时忽略这两个信号，以避免把已成功发布的文件误报为中断。强制终止和断电不能保证这一点；再次运行时按现有文件和记录检查，不自动覆盖。

默认无覆盖发布使用同磁盘硬链接，以保证目标在下载期间被其他程序创建时也不会被覆盖。对于不支持硬链接的文件系统（某些可移动盘/网络盘），会保留候选文件并报错，而不是退化成可能留下半文件或覆盖其他文件的复制。可换用支持的目标磁盘；不要为了规避错误擅自添加 `--overwrite`。

若单线程任务在成功接收后仍无总大小也无可信哈希，返回 4，保留文件供检查。不要通过关闭校验把它改报成功。

## 自定义 skill 仓库

本 skill 还提供仓库初始化脚本，用于把当前 skill 作为第一个 skill 放进 `~/project/skills`：

```bash
python3 "<SKILL_DIR>/scripts/init_skill_repo.py" --json
```

它会安全地创建以下布局，并在本地建立初始 Git 提交：

```text
~/project/skills/
├── .git/
├── README.md
├── .gitignore
└── skills/
    └── multithread-downloader/
        └── SKILL.md
```

已经存在的仓库和文件不会被覆盖；若 `skills/multithread-downloader/` 已存在，脚本会停止并报告冲突。
要准备空仓库，使用 `--empty-repo`。新 skill 可以在仓库内执行：

```bash
cd ~/project/skills
npx skills init skills/my-skill
```

完成编辑后提交并推送到 GitHub。`npx skills` 安装的是 Git 仓库中可发现的 skill 目录，不要求把整个仓库发布成 npm 包：

```bash
npx skills add <github-owner>/<repository> --skill my-skill
# 或
npx skills add <github-owner>/<repository> --all
```

脚本不会自动创建 GitHub 远程仓库；远程地址、认证和 `git push` 由用户自行决定。
