# Edge Region Tool / Edge 地区修复工具

A Windows utility for inspecting and updating Microsoft Edge region settings, with automatic backups and restore support.

用于检查和修改 Edge 本地功能配置里的国家记录，辅助排查 Copilot 入口与“帮我写作 / Inline Compose”因地区判定而消失的问题。支持 US、JP、HK、GB 等两位地区代码。

**当前工具版本：2.0。它不更改 IP、账号地区或服务端资格，不保证所有地区和版本都能恢复功能。**

## 下载与首次使用

1. 点击本页 **Code → Download ZIP**，解压到一个独立文件夹。
2. 安装 Python，推荐 **Python 3.12**，确保终端中的 `python` 命令可用。
3. 双击 **安装依赖.cmd**，等待安装成功。只会将 `zstandard` 安装到本工具的 `lib` 目录。
4. 保存网页里未提交的内容，完全退出 Edge，包括后台进程。
5. 双击 **打开工具.cmd**，按菜单查看、修改地区或恢复备份。

也可以在解压目录执行：

```powershell
python -m pip install --target .\lib -r requirements.txt
python .\edge_region.py status
python .\edge_region.py set US
```

只读查看可以在 Edge 运行时执行；修改和恢复时工具会检查 Edge 是否已退出，不会自动结束浏览器。

## 三个入口有什么区别？

| 入口 | 行为 |
| --- | --- |
| 打开工具.cmd | 交互菜单：查看状态、选择地区、修复并启动、恢复备份 |
| 修复为US.cmd | 只将相关国家记录改为 US |
| 修复并启动Edge.cmd | 改为 US，并显式修复已知写作禁用条目，然后正常启动 Edge |

第三种方式对应 `launch US --repair-writing`。它只处理代码中明确识别的 `EdgeWritingBlockedRegionsMitigation20261103` 条目里的 `msEdgeComposeNext`；不会猜测新规则。

## 备份与恢复

发生实际修改前会备份原文件，位置：

```text
%LOCALAPPDATA%\Microsoft\Edge\User Data\Codex-Backups\RegionTool\时间戳
```

恢复最近一次备份：

```powershell
python .\edge_region.py restore latest
```

恢复是还原完整文件，适合撤销近期修改；不要用旧备份覆盖跨版本的新配置。工具会检查版本、文件校验值和浏览器运行状态。

## 修改范围

- `Local State` 中的国家记录。
- `VariationsSeedV2` 中的国家字段与响应头国家。
- 最新配置通过 `safe_seed_content` 引用备用配置时，同步处理 `VariationsSafeSeedV2`。
- 只有明确使用 `--repair-writing` 时才修改上述已知写作规则。

不修改注册表、文件权限、Windows 服务或计划任务。没有后台守护。Edge 后续重新下载配置仍可能覆盖地区记录。

## 支持与验证边界

| 验证范围 | 状态 |
| --- | --- |
| Edge 157.0.4341.60 写作地区判定 | 开发期间做过单项重启对照 |
| Edge 158.0.4370.33 文件结构 | 用真实文件副本验证读取、US/JP 切换、备用引用保留及三文件恢复 |
| Edge 158 运行时写作功能 | 未在 2.0 修复后重新完成界面验收 |
| 离线回归 | 14 项测试通过 |
| 其他 Edge 主版本 | 默认拒绝写入，需先核实结构 |

开发期间的验证不代表所有机器、账号或后续 Edge 版本均适用。不要仅为跳过报错而使用 `--allow-new-version`。

## 常见问题

- **“未完成：Edge 仍在运行”**：保存工作并完全退出 Edge 后重试。
- **无法加载 Zstandard**：先运行安装依赖脚本，确认安装与执行使用的是同一个 Python。
- **缺少配置、结构异常、非空签名**：停止操作，保留现场；不要用文本编辑器全局替换或拿他人的配置覆盖。
- **修复后仍无写作入口**：还需检查账号、策略、服务端资格和功能是否仍受支持；本工具不承诺强制开通。

完整字段说明及 CLI 用法见 [使用说明.md](使用说明.md)。

## 测试

```powershell
python -B -m unittest -v test_edge_region
```

测试使用临时文件和模拟浏览器状态，不修改实际浏览器配置。

## Privacy and license

公开源码不包含浏览器配置、Cookie、个人备份或诊断日志。请勿在 Issue 中上传完整 `Local State` 或个人配置文件。

本项目使用 [MIT License](LICENSE)。依赖 `zstandard` 按其自身许可证分发，本仓库通过 PyPI 安装它，不重新打包其原生二进制文件。

## References

- [Chromium StoredSeedInfo schema](https://raw.githubusercontent.com/chromium/chromium/main/components/variations/proto/stored_seed_info.proto)
- [Chromium seed storage and safe-seed references](https://raw.githubusercontent.com/chromium/chromium/main/components/variations/seed_reader_writer.h)
- [Microsoft Edge configuration and experiments](https://learn.microsoft.com/en-us/deployedge/edge-configuration-and-experiments)
