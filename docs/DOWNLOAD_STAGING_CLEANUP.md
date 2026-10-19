# 下载暂存自动清理

服务器可以使用 systemd 定时器清理下载工具留下的暂存副本，同时保留正式文献库文件。

## 清理范围

只扫描项目根目录 `outputs/` 下名称匹配以下精确前缀的直接子目录：

- `download-staging-*`
- `qingli-downloads-*`
- `ablesci-downloads-*`

新下载任务统一使用 `outputs/download-staging-<任务名>/`。脚本不递归扫描，不处理指向目录的符号链接，也不会扫描 `data/`、正式文献库文件、数据库备份或 `outputs/default-library-*`。`outputs/` 与 `data/storage/` 发生重叠或任一根目录是符号链接时，脚本拒绝执行。

下载、校验清单和后续写入全部完成后，下载任务必须在该目录中创建空文件 `.download-complete`。定时任务只处理有完成标记的目录；缺少标记时整个目录保持不动。

```bash
cd /opt/AI-shujvku/literature-ai
touch outputs/download-staging-<任务名>/.download-complete
```

## 删除条件

一个 PDF 必须同时满足以下条件才会删除：

1. 暂存目录最后一次发生目录项变化已超过 `.download-complete` 创建后的 7 天观察期；源 PDF 自带的历史时间戳不会提前触发清理。
2. PDF 和校验清单在完成标记创建后没有再次发生内容或元数据变更。
3. 文件是所在暂存目录中的普通文件，不是符号链接或 FIFO。
4. 数据库中已有相同 SHA-256 和大小的登记文件。
5. 数据库指向的 `data/storage/` 正式副本实际存在，大小与 SHA-256 再次核对一致。

未入库、哈希不一致、文件仍在变化或路径不安全的文件都会保留并写入日志。校验清单必须与本次验证通过的 PDF 文件名、大小和哈希完全对应，否则也会保留。暂存目录只有在所有条目均已安全删除时才会移除。

清理先完成整批只读规划，再进入执行。目录中任何一个关键文件无法验证时，该目录本轮不删除任何文件。执行时先把目标原子移动到目录内的随机隔离名，随后重新核对 inode、大小和内容哈希；恢复隔离文件使用不覆盖目标的硬链接操作。

正式文献删除接口和暂存清理器共享 `data/storage/.artifact-delete.lock`。清理器在最终验证正式副本并删除暂存隔离文件期间持有该锁；应用的 PDF 删除流程也会取得同一锁。正式副本在锁外消失、目录或完成标记变化时，暂存文件会恢复并保留。

## 定时执行

systemd 系统定时器使用主机本地时区；当前 `master` 已确认为 `Asia/Shanghai`，每天 03:30 左右运行，带最多 15 分钟随机延迟。服务以 `2401liyuhao` 用户执行；一小时内最多启动 3 次，等同首次失败后最多再重试两次。

服务只执行只读发布目录中的脚本：`deploy-state/download-staging-cleanup/current/cleanup_download_staging.py`。该目录由明确 Git 提交生成，不执行带未提交修改的开发工作树脚本。

部署脚本在 `plan` 时记录本次提交内清理脚本、service 和 timer 的 SHA-256；`apply` 成功重启应用后，从该提交的只读发行目录原子生成 `deploy-state/download-staging-cleanup/<commit>` 并切换 `current` 符号链接。最终 `verify` 同时核对容器版本、后端健康状态、`current` 指向及三个发布文件哈希。

确认发布回执后，可按以下方式安装系统级单元：

```bash
sudo install -m 0644 /opt/AI-shujvku/literature-ai/deploy-state/download-staging-cleanup/current/litai-download-staging-cleanup.service /etc/systemd/system/litai-download-staging-cleanup.service
sudo install -m 0644 /opt/AI-shujvku/literature-ai/deploy-state/download-staging-cleanup/current/litai-download-staging-cleanup.timer /etc/systemd/system/litai-download-staging-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now litai-download-staging-cleanup.timer
```

```bash
systemctl status litai-download-staging-cleanup.timer
systemctl list-timers litai-download-staging-cleanup.timer
sudo systemctl start litai-download-staging-cleanup.service
sudo journalctl -u litai-download-staging-cleanup.service -n 100 --no-pager
```

运行日志位于 `outputs/cleanup-logs/download-staging-cleanup.log`，最多保留 5 个 2 MiB 轮转文件。

## 手动预演和执行

默认只预演，不删除：

```bash
cd /opt/AI-shujvku/literature-ai
python3 scripts/cleanup_download_staging.py --retention-days 7
python3 scripts/cleanup_download_staging.py --retention-days 7 --apply
```

预演与正式执行使用同一份规划结果，因此预演会同时列出 PDF、可验证的校验清单、完成标记和空目录动作。数据库不可用或规划阶段异常时，脚本失败退出且不执行任何删除。同一暂存目录中只要一个关键文件在规划后发生变化，该目录本轮就停止执行并保留尚未删除的内容。
