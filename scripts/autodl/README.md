# AutoDL / 云 GPU 运维脚本

实现位于此目录；**仓库根目录保留同名转发器**，兼容旧文档与自定义镜像里写死的路径。

## 文件

| 文件 | 说明 |
|------|------|
| `apply_lora_next_anima_defaults.py` | AutoDL 路径下 tokenizer / 默认采样词等 |
| `start_lora_next.sh` | conda `lora-next` + 清端口 + 启动 GUI（6006） |
| `autostart_lora_gui.sh` / `restart_lora_gui.sh` | 后台拉起 / 重启 |
| `update_lora_gui.sh` | `git pull` + defaults + restart |

## 与根目录 `start_autodl.sh` 的区别

| 入口 | 场景 |
|------|------|
| **`/…/start_autodl.sh`（根目录，勿挪）** | 镜像开机绑定；薄脚本，端口 6006 |
| `scripts/autodl/start_lora_next.sh` | 已配置 conda 的完整运维链 |

编辑逻辑请改本目录；根目录 `start_lora_next.sh` 等仅 `exec` 转发。

## 启动权限

根目录转发器和运维脚本之间使用 `bash` 调用，不依赖子脚本的执行位。旧版代码若报 `Permission denied`，可先在仓库根目录运行 `chmod +x scripts/autodl/*.sh`，再执行 `bash autostart_lora_gui.sh`。启动日志位于 `/root/lora_gui_autostart.log`。

## 自动关机命令兼容

部分云镜像中的 `shutdown` 是未带 shebang 的 shell 脚本，直接从 Python 启动可能报 `[Errno 8] Exec format error`。后端仅在 Linux 收到该错误且文件为文本时，使用 Bash 解释该命令脚本；仍会检查退出码并在队列面板显示失败信息。排查时可运行 `command -v shutdown` 和 `file "$(command -v shutdown)"` 查看命令路径与文件类型，无需执行关机。
